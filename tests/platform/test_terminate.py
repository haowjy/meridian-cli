from __future__ import annotations

import asyncio
import sys
import textwrap

import psutil
import pytest

from meridian.lib.platform.process_scope.fallback import _snapshot_tree
from meridian.lib.platform.terminate import terminate_tree
from tests.conftest import windows_only

_PARENT_WITH_CHILD = textwrap.dedent(
    """
    import subprocess
    import sys
    import time

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    print(child.pid, flush=True)
    time.sleep(60)
    """
).strip()


async def _spawn_parent_with_child() -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _PARENT_WITH_CHILD,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _read_child_pid(process: asyncio.subprocess.Process) -> int:
    assert process.stdout is not None
    line = await asyncio.wait_for(process.stdout.readline(), timeout=5)
    assert line, "child PID was not emitted by parent process"
    return int(line.decode("utf-8").strip())


async def _wait_for_snapshot_child(
    *,
    parent_pid: int,
    child_pid: int,
    timeout: float = 5.0,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        root, children = _snapshot_tree(parent_pid)
        if root is not None and any(proc.pid == child_pid for proc in children):
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"timed out waiting for child {child_pid} in snapshot of {parent_pid}")


async def _wait_for_pid_exit(pid: int, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if not psutil.pid_exists(pid):
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"timed out waiting for process {pid} to exit")


@pytest.mark.asyncio
async def test_snapshot_tree_returns_process_and_children() -> None:
    process = await _spawn_parent_with_child()
    child_pid = await _read_child_pid(process)
    try:
        await _wait_for_snapshot_child(parent_pid=process.pid, child_pid=child_pid)
        root, children = _snapshot_tree(process.pid)
        assert root is not None
        assert root.pid == process.pid
        assert any(proc.pid == child_pid for proc in children)
    finally:
        await terminate_tree(process, grace_secs=0.1)
        await asyncio.wait_for(process.wait(), timeout=5)


@pytest.mark.asyncio
async def test_terminate_tree_terminates_parent_and_child_processes() -> None:
    process = await _spawn_parent_with_child()
    child_pid = await _read_child_pid(process)
    await _wait_for_snapshot_child(parent_pid=process.pid, child_pid=child_pid)

    await terminate_tree(process, grace_secs=0.1)
    await asyncio.wait_for(process.wait(), timeout=5)

    assert process.returncode is not None
    await _wait_for_pid_exit(child_pid)


@pytest.mark.asyncio
@windows_only
async def test_terminate_tree_windows_terminate_is_kill_semantics() -> None:
    """Windows note: psutil terminate() maps to kill() so grace periods are not semantic."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
    )

    await terminate_tree(process, grace_secs=5.0)
    await asyncio.wait_for(process.wait(), timeout=5)

    assert process.returncode is not None
