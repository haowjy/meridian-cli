"""Managed stdio process ownership tests."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import psutil
import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections import managed_stdio
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.harness.connections.errors import (
    IncompleteStartupTeardown,
    RetryableConnectionStartupError,
    TeardownStatus,
)
from meridian.lib.platform.process_scope import CleanupResult, ScopedProcessHandle
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.spawn_store import start_spawn


@pytest.mark.asyncio
async def test_launch_managed_stdio_reaps_child_when_registration_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_id = SpawnId("stdio-registration-failure")
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="chat-1",
        model="test-model",
        agent="tester",
        harness="pi",
        prompt="test",
        status="running",
    )
    launched: list[asyncio.subprocess.Process] = []

    async def fail_registration(**kwargs: object) -> object:
        launched.append(kwargs["process"])  # type: ignore[arg-type]
        raise RuntimeError("injected registration failure")

    monkeypatch.setattr(
        managed_stdio,
        "register_spawn_owned_process",
        fail_registration,
    )

    try:
        with pytest.raises(RuntimeError, match="injected registration failure"):
            await managed_stdio.launch_managed_stdio(
                config=ConnectionConfig(
                    spawn_id=spawn_id,
                    harness_id=HarnessId.PI,
                    prompt="test",
                    control_root=tmp_path,
                    child_env={},
                    runtime_root=runtime_root,
                ),
                harness_id=HarnessId.PI,
                command=(sys.executable, "-c", "import time; time.sleep(60)"),
                env=os.environ.copy(),
                cwd=str(tmp_path),
                stdin=asyncio.subprocess.PIPE,
                stdout_limit=64 * 1024,
                kill_grace_seconds=0.1,
                terminate_reason="test_cleanup",
            )

        assert len(launched) == 1
        await asyncio.wait_for(launched[0].wait(), timeout=1.0)
        stderr_log = runtime_root / "spawns" / str(spawn_id) / "stderr.log"
        assert stderr_log.resolve() not in {
            Path(open_file.path).resolve()
            for open_file in psutil.Process().open_files()
        }
    finally:
        for process in launched:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()


@pytest.mark.asyncio
async def test_launch_managed_stdio_preserves_incomplete_provisional_teardown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_id = SpawnId("stdio-incomplete-registration-cleanup")
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="chat-1",
        model="test-model",
        agent="tester",
        harness="claude",
        prompt="test",
        status="running",
    )
    launched: list[asyncio.subprocess.Process] = []

    async def fail_registration(**kwargs: object) -> object:
        launched.append(kwargs["process"])  # type: ignore[arg-type]
        raise RetryableConnectionStartupError("temporary registration failure")

    async def abandon_cleanup(*_args: object, **_kwargs: object) -> TeardownStatus:
        return TeardownStatus.ABANDONED

    monkeypatch.setattr(managed_stdio, "register_spawn_owned_process", fail_registration)
    monkeypatch.setattr(managed_stdio, "reap_on_ownership_transfer_failure", abandon_cleanup)

    try:
        with pytest.raises(IncompleteStartupTeardown) as raised:
            await managed_stdio.launch_managed_stdio(
                config=ConnectionConfig(
                    spawn_id=spawn_id,
                    harness_id=HarnessId.CLAUDE,
                    prompt="test",
                    control_root=tmp_path,
                    child_env={},
                    runtime_root=runtime_root,
                ),
                harness_id=HarnessId.CLAUDE,
                command=(sys.executable, "-c", "import time; time.sleep(60)"),
                env=os.environ.copy(),
                cwd=str(tmp_path),
                stdin=asyncio.subprocess.PIPE,
                stdout_limit=64 * 1024,
                kill_grace_seconds=0.1,
                terminate_reason="test_cleanup",
            )

        assert raised.value.teardown is TeardownStatus.ABANDONED
        assert str(raised.value) == "temporary registration failure"
    finally:
        for process in launched:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()


@pytest.mark.asyncio
async def test_failed_scope_cleanup_retains_managed_process_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_id = SpawnId("stdio-retained-failed-cleanup")
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="chat-1",
        model="test-model",
        agent="tester",
        harness="claude",
        prompt="test",
        status="running",
    )
    child = await managed_stdio.launch_managed_stdio(
        config=ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.CLAUDE,
            prompt="test",
            control_root=tmp_path,
            child_env={},
            runtime_root=runtime_root,
        ),
        harness_id=HarnessId.CLAUDE,
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
        env=os.environ.copy(),
        cwd=str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
        stdout_limit=64 * 1024,
        kill_grace_seconds=0.1,
        terminate_reason="test_cleanup",
    )
    process = child.process
    assert process is not None

    async def report_failure(
        handle: ScopedProcessHandle,
        grace_seconds: float = 5.0,
        reason: str = "stop_called",
    ) -> CleanupResult:
        return CleanupResult(
            scope_id=handle.snapshot.scope_id,
            root_pid=handle.pid,
            descendant_count=None,
            reason=reason,
            grace_seconds=grace_seconds,
            kill_escalated=False,
            degraded_fallback=True,
            skip_reason="pid_reuse_detected",
        )

    monkeypatch.setattr(ScopedProcessHandle, "terminate", report_failure)
    try:
        cleanup = await child.terminate()
        assert cleanup.teardown is TeardownStatus.FAILED
        assert child.process is process
        assert child.scope_snapshot is not None
        assert process.returncode is None
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        child.close_stderr_handle()


@pytest.mark.asyncio
async def test_cancellation_during_root_reap_propagates_and_retains_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller cancellation wins while managed ownership remains available."""

    spawn_id = SpawnId("stdio-cancelled-root-reap")
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="chat-1",
        model="test-model",
        agent="tester",
        harness="claude",
        prompt="test",
        status="running",
    )
    child = await managed_stdio.launch_managed_stdio(
        config=ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.CLAUDE,
            prompt="test",
            control_root=tmp_path,
            child_env={},
            runtime_root=runtime_root,
        ),
        harness_id=HarnessId.CLAUDE,
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
        env=os.environ.copy(),
        cwd=str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
        stdout_limit=64 * 1024,
        kill_grace_seconds=0.1,
        terminate_reason="test_cleanup",
    )
    process = child.process
    assert process is not None
    original_wait = process.wait
    root_reap_started = asyncio.Event()
    keep_waiting = asyncio.Event()

    async def report_verified_empty_scope(
        handle: ScopedProcessHandle,
        grace_seconds: float = 5.0,
        reason: str = "stop_called",
    ) -> CleanupResult:
        return CleanupResult(
            scope_id=handle.snapshot.scope_id,
            root_pid=handle.pid,
            descendant_count=0,
            reason=reason,
            grace_seconds=grace_seconds,
            kill_escalated=False,
            degraded_fallback=False,
            skip_reason=None,
            survivor_count=0,
            verification_complete=True,
        )

    async def wait_until_cancelled() -> int:
        root_reap_started.set()
        await keep_waiting.wait()
        return 0

    monkeypatch.setattr(ScopedProcessHandle, "terminate", report_verified_empty_scope)
    monkeypatch.setattr(process, "wait", wait_until_cancelled)
    try:
        cleanup_task = asyncio.create_task(child.terminate())
        await asyncio.wait_for(root_reap_started.wait(), timeout=1.0)
        cleanup_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cleanup_task

        assert child.process is process
        assert child.scope_snapshot is not None
        assert process.returncode is None
    finally:
        monkeypatch.setattr(process, "wait", original_wait)
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await original_wait()
        child.close_stderr_handle()
