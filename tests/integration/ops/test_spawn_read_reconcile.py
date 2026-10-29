from __future__ import annotations

import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.spawn.models import (
    SpawnListInput,
    SpawnShowInput,
    SpawnStatsInput,
    SpawnWaitInput,
)
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.streaming.control_socket import socket_directory
from tests.conftest import posix_only
from tests.support import legacy_rows
from tests.support.launch_paths import launch_paths

if TYPE_CHECKING:
    import pytest

pytestmark = posix_only

_DRIVER = Path(__file__).parents[2] / "support" / "launch_lock_driver.py"


def _state_root(project_root: Path) -> Path:
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


def _seed_running_spawn(runtime_root: Path, spawn_id: str) -> None:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c1",
        model="gpt-5.3-codex",
        agent="coder",
        harness="codex",
        prompt="hello",
        runner_pid=os.getpid(),
    )


def _until(check: Callable[[], Any], *, timeout: float = 20.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("bounded observation did not become true")


def _launch_lock_is_free(lock_path: Path) -> bool:
    with lock_path.open("rb") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    return True


def _kill_process_group(pgid: int) -> None:
    with suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


def test_spawn_show_sync_renders_finalizing_status_without_partial_terminal_error(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_running_spawn(runtime_root, "p1")
    legacy_rows.record_exited(
        runtime_root,
        "p1",
        exit_code=143,
        exited_at="2026-04-12T14:00:00Z",
    )
    legacy_rows.mark_finalizing(runtime_root, "p1")

    output = spawn_api.spawn_show_sync(
        SpawnShowInput(
            spawn_id="p1",
            include_report_body=False,
            project_root=project_root.as_posix(),
        )
    )

    assert output.spawn_id == "p1"
    assert output.status == "finalizing"
    assert output.last_attempt_exited_at == "2026-04-12T14:00:00Z"
    assert output.last_attempt_exit_code == 143
    rendered = output.format_text()
    assert "Status: finalizing" in rendered
    verbose_rendered = output.format_text(FormatContext(verbosity=1))
    assert "Status: finalizing (cleanup in progress)" in verbose_rendered
    assert "orphan_finalization" not in verbose_rendered
    assert "awaiting finalization" not in verbose_rendered


def test_dead_runner_with_reused_pid_is_not_running(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    unrelated = subprocess.Popen(["sleep", "30"])
    try:
        unrelated_birth = psutil.Process(unrelated.pid).create_time()
        spawn_store.start_spawn(
            runtime_root,
            spawn_id="p1",
            chat_id="c1",
            model="test",
            agent="coder",
            harness="codex",
            prompt="hello",
            runner_pid=unrelated.pid,
            runner_created_at_epoch=unrelated_birth - 1.0,
            started_at="2000-01-01T00:00:00Z",
        )
        (runtime_root / "spawns" / "p1" / "launch.lock").touch()

        output = spawn_api.spawn_show_sync(
            SpawnShowInput(spawn_id="p1", project_root=project_root.as_posix())
        )

        assert output.status == "failed"
        assert output.failure_reason == "crashed"
        listed = spawn_api.spawn_list_sync(
            SpawnListInput(statuses=(), project_root=project_root.as_posix())
        )
        assert [(row.spawn_id, row.status) for row in listed.spawns] == [("p1", "failed")]
        stats = spawn_api.spawn_stats_sync(SpawnStatsInput(project_root=project_root.as_posix()))
        assert stats.failed == 1
        waited = spawn_api.spawn_wait_sync(
            SpawnWaitInput(spawn_ids=("p1",), project_root=project_root.as_posix())
        )
        assert [(row.spawn_id, row.status) for row in waited.spawns] == [("p1", "failed")]
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait(timeout=5)


def test_sigkilled_runner_shows_failed_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n')
    runtime_root = tmp_path / "runtime"
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(_DRIVER))} harness "$@"\n'
    )
    fake_claude.chmod(0o755)
    package_root = Path(__file__).parents[3]
    env = {
        "HOME": str(tmp_path / "user"),
        "USER": os.environ.get("USER", "test"),
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "PYTHONPATH": f"{package_root}:{package_root / 'src'}",
        "FAKE_ROOT": str(tmp_path),
        "FAKE_SOCKET_ROOT": str(socket_directory().parent),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "native"),
        "MERIDIAN_HOME": str(tmp_path / "home"),
        "_MERIDIAN_RUNTIME_DIR": str(runtime_root),
    }
    parent = subprocess.Popen(
        [sys.executable, str(_DRIVER), "spawn", "-m", "haiku", "-p", "hold", "--bg"],
        cwd=project_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    runner_pid: int | None = None
    harness_pgid: int | None = None
    try:
        stdout, stderr = parent.communicate(timeout=30)
        assert parent.returncode == 0, (stdout, stderr)
        _until(lambda: (tmp_path / "p1.worker").exists())
        state_path = runtime_root / "spawns" / "p1" / "state.json"
        state = json.loads(state_path.read_text())
        runner_pid = int(state["runner_pid"])
        scopes_path = launch_paths(runtime_root, "p1").process_scopes
        scopes = _until(
            lambda: json.loads(scopes_path.read_text()) if scopes_path.exists() else None
        )
        harness_pgid = int(scopes["scopes"][0]["pgid"])
        os.kill(runner_pid, signal.SIGKILL)
        _kill_process_group(harness_pgid)
        lock_path = runtime_root / "spawns" / "p1" / "launch.lock"
        _until(lambda: _launch_lock_is_free(lock_path), timeout=5)
        monkeypatch.setenv("_MERIDIAN_RUNTIME_DIR", str(runtime_root))
        monkeypatch.setenv("_MERIDIAN_DEPTH", "0")

        output = spawn_api.spawn_show_sync(
            SpawnShowInput(spawn_id="p1", project_root=project_root.as_posix())
        )

        assert output.status == "failed"
        assert output.failure_reason == "crashed"
    finally:
        (tmp_path / "p1.exit").touch()
        cleanup_pids: set[int] = set()
        for pid_path in (tmp_path / "p1.pid", tmp_path / "p1.worker"):
            with suppress(OSError, ValueError):
                cleanup_pids.add(int(pid_path.read_text()))
        state_path = runtime_root / "spawns" / "p1" / "state.json"
        with suppress(OSError, TypeError, ValueError, KeyError):
            cleanup_pids.add(int(json.loads(state_path.read_text())["runner_pid"]))
        if runner_pid is not None:
            cleanup_pids.add(runner_pid)
        for pid in cleanup_pids:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        if harness_pgid is not None:
            _kill_process_group(harness_pgid)
        if parent.poll() is None:
            _kill_process_group(parent.pid)
            parent.wait(timeout=5)
