from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

from meridian.lib.platform import parent_watchdog
from meridian.lib.platform.detached_process import start_parent_watchdog
from meridian.lib.platform.process_scope.base import (
    PROCESS_BIRTH_UNKNOWN_EPOCH,
    ProcessScopeSnapshot,
)
from tests.conftest import posix_only

if TYPE_CHECKING:
    import pytest


def _process_is_running(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _wait_for_file(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 0:
            return
        time.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {path}")


def test_watchdog_entrypoint_imports_only_stdlib_and_psutil() -> None:
    script_path = Path(parent_watchdog.__file__)
    probe = """
import json
import runpy
import sys
from pathlib import Path

script_path = Path(sys.argv[1])
sys.path.insert(0, str(script_path.parent))
before = set(sys.modules)
runpy.run_path(str(script_path), run_name="_parent_watchdog_import_probe")
added = set(sys.modules) - before
print(json.dumps({
    "meridian": sorted(
        name for name in sys.modules
        if name == "meridian" or name.startswith("meridian.")
    ),
    "unexpected": sorted(
        name for name in added
        if name.split(".", 1)[0] not in sys.stdlib_module_names
        and name.split(".", 1)[0] not in {"psutil", "scope_termination"}
    ),
}))
"""

    completed = subprocess.run(
        (sys.executable, "-c", probe, str(script_path)),
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {"meridian": [], "unexpected": []}


@posix_only
def test_parent_death_link_waits_for_watchdog_readiness() -> None:
    watchdog = start_parent_watchdog()
    try:
        assert watchdog is not None
        assert watchdog.process.poll() is None
    finally:
        if watchdog is not None:
            watchdog.close()


@posix_only
def test_watchdog_refuses_scope_without_birth_time() -> None:
    watchdog = start_parent_watchdog()
    assert watchdog is not None
    scope = ProcessScopeSnapshot(
        scope_id="backend",
        owner_policy="spawn_owned",
        owner_id="watchdog-test",
        role="harness_backend",
        containment="pid_tree_fallback",
        root_pid=os.getpid(),
        root_created_at_epoch=PROCESS_BIRTH_UNKNOWN_EPOCH,
        pgid=None,
        job_name=None,
        degraded_reason=None,
    )
    try:
        assert watchdog.register_scope(scope) is False
    finally:
        watchdog.close()


def test_watchdog_release_removes_registered_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = ProcessScopeSnapshot(
        scope_id="backend",
        owner_policy="spawn_owned",
        owner_id="watchdog-test",
        role="harness_backend",
        containment="pid_tree_fallback",
        root_pid=123,
        root_created_at_epoch=456.0,
        pgid=None,
        job_name=None,
        degraded_reason=None,
    )
    read_fd, write_fd = os.pipe()
    messages = (
        json.dumps({"action": "register", "scope": scope.__dict__})
        + "\n"
        + json.dumps({"action": "release", "release_id": scope.release_id})
        + "\n"
    ).encode()
    os.write(write_fd, messages)
    os.close(write_fd)
    parent_checks = iter((True, False))
    terminated: list[object] = []
    monkeypatch.setattr(
        parent_watchdog,
        "process_is_same_birth",
        lambda _parent: next(parent_checks),
    )
    monkeypatch.setattr(
        parent_watchdog,
        "terminate_scope_sync",
        lambda scope, **_kwargs: terminated.append(scope),
    )

    try:
        result = parent_watchdog.watch_parent_scopes(
            parent=parent_watchdog.WatchedProcess(pid=1, created_at_epoch=1.0),
            control_fd=read_fd,
            poll_interval_seconds=0.05,
            grace_seconds=0.0,
        )
    finally:
        os.close(read_fd)

    assert result == 0
    assert terminated == []


@posix_only
def test_watchdog_kills_backend_wrapper_group_when_launcher_is_killed(
    tmp_path: Path,
) -> None:
    root_pid_path = tmp_path / "backend-wrapper.pid"
    grandchild_pid_path = tmp_path / "backend-grandchild.pid"
    parent_code = """
import asyncio
import os
import sys
from pathlib import Path

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.managed_backend import (
    ManagedBackendConfig,
    launch_managed_backend,
)
from meridian.lib.platform.detached_process import start_parent_watchdog
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.spawn_store import start_spawn

async def main():
    root_pid_path, grandchild_pid_path, control_root = map(Path, sys.argv[1:])
    spawn_id = SpawnId("p-watchdog-backend")
    runtime_root = resolve_project_runtime_root_for_write(control_root)
    start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="watchdog-test",
        model="test",
        agent="tester",
        harness="codex",
        prompt="test",
        status="running",
    )
    watchdog = start_parent_watchdog()
    if watchdog is None:
        raise RuntimeError("watchdog unavailable")
    handle = await launch_managed_backend(
        ManagedBackendConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.CODEX,
            command=(
                "sh",
                "-c",
                'sleep 300 & echo $! > "$1"; wait',
                "sh",
                str(grandchild_pid_path),
            ),
            cwd=control_root,
            env=os.environ.copy(),
            control_root=control_root,
            parent_watchdog=watchdog,
        ),
        stderr=asyncio.subprocess.DEVNULL,
    )
    root_pid_path.write_text(str(handle.process.pid))
    await handle.process.wait()

asyncio.run(main())
""".strip()
    parent = subprocess.Popen(
        (
            sys.executable,
            "-c",
            parent_code,
            str(root_pid_path),
            str(grandchild_pid_path),
            str(tmp_path),
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    root_pid = 0
    grandchild_pid = 0
    try:
        _wait_for_file(root_pid_path)
        _wait_for_file(grandchild_pid_path)
        root_pid = int(root_pid_path.read_text(encoding="utf-8"))
        grandchild_pid = int(grandchild_pid_path.read_text(encoding="utf-8"))

        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5.0)

        deadline = time.monotonic() + 8.0
        while (
            _process_is_running(root_pid) or _process_is_running(grandchild_pid)
        ) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _process_is_running(root_pid) is False
        assert _process_is_running(grandchild_pid) is False
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5.0)
        for pid in (root_pid, grandchild_pid):
            if pid > 0 and _process_is_running(pid):
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
