from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import psutil

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.ops.spawn.models import (
    SpawnListInput,
    SpawnShowInput,
    SpawnStatsInput,
    SpawnWaitInput,
)
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from tests.conftest import posix_only

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


