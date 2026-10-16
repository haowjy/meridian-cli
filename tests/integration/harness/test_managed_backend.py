"""Managed backend launch helper tests."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import psutil
import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections import managed_backend
from meridian.lib.harness.connections.managed_backend import (
    ManagedBackendConfig,
    launch_managed_backend,
)
from meridian.lib.platform import launch_gate
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.process_scope_projection import read_scopes_from_disk
from meridian.lib.state.spawn_store import finalize_spawn, start_spawn


def _publish_spawn(tmp_path: Path, spawn_id: SpawnId, *, status: str = "running") -> None:
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="chat-1",
        model="gpt-5.4",
        agent="tester",
        harness="codex",
        prompt="test",
        status="running",
    )
    if status == "succeeded":
        finalize_spawn(runtime_root, spawn_id, "succeeded", 0, origin="runner")


@pytest.mark.asyncio
async def test_launch_managed_backend_records_scope(tmp_path: Path) -> None:
    spawn_id = SpawnId("spawn-3")
    _publish_spawn(tmp_path, spawn_id)

    handle = await launch_managed_backend(
        ManagedBackendConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.CODEX,
            command=(sys.executable, "-c", "pass"),
            cwd=tmp_path,
            env=os.environ.copy(),
            control_root=tmp_path,
            grace_seconds=0.1,
        ),
        stderr=asyncio.subprocess.DEVNULL,
    )

    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    scopes = read_scopes_from_disk(runtime_root, spawn_id)

    assert [(scope.scope_id, scope.root_pid) for scope in scopes] == [
        ("backend", handle.process.pid)
    ]
    await handle.process.wait()


@pytest.mark.asyncio
async def test_launch_managed_backend_refuses_unknown_birth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_id = SpawnId("spawn-unknown-birth")
    _publish_spawn(tmp_path, spawn_id)

    class _InaccessibleProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def create_time(self) -> float:
            raise psutil.AccessDenied(pid=self.pid)

    monkeypatch.setattr(launch_gate.psutil, "Process", _InaccessibleProcess)
    marker = tmp_path / "executed"
    with pytest.raises(RuntimeError, match="birth time"):
        await launch_managed_backend(
            ManagedBackendConfig(
                spawn_id=spawn_id,
                harness_id=HarnessId.OPENCODE,
                command=("/bin/sh", "-c", f"touch {marker}"),
                cwd=tmp_path,
                env=os.environ.copy(),
                control_root=tmp_path,
                grace_seconds=0.1,
            ),
            stderr=asyncio.subprocess.DEVNULL,
        )
    assert not marker.exists()
    assert not read_scopes_from_disk(resolve_project_runtime_root_for_write(tmp_path), spawn_id)


@pytest.mark.asyncio
async def test_launch_managed_backend_cleans_up_when_terminal_spawn_rejects_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_id = SpawnId("terminal-spawn")
    _publish_spawn(tmp_path, spawn_id, status="succeeded")
    launched: list[asyncio.subprocess.Process] = []
    original_launch = asyncio.create_subprocess_exec

    async def capture_launch(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        process = await original_launch(*args, **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(managed_backend.asyncio, "create_subprocess_exec", capture_launch)

    try:
        with pytest.raises(ValueError, match="after cleanup began"):
            await launch_managed_backend(
                ManagedBackendConfig(
                    spawn_id=spawn_id,
                    harness_id=HarnessId.CODEX,
                    command=(sys.executable, "-c", "import time; time.sleep(60)"),
                    cwd=tmp_path,
                    env=os.environ.copy(),
                    control_root=tmp_path,
                    grace_seconds=0.1,
                ),
                stderr=asyncio.subprocess.DEVNULL,
            )

        assert len(launched) == 1
        # psutil can observe exit before asyncio's child watcher sets returncode.
        await asyncio.wait_for(launched[0].wait(), timeout=1.0)
        assert launched[0].returncode is not None
    finally:
        for process in launched:
            if process.returncode is None:
                process.kill()
                await process.wait()


@pytest.mark.asyncio
async def test_scope_termination_disarms_guardian_without_waiting_its_grace(tmp_path: Path) -> None:
    import time

    from tests.support.async_determinism import wait_until

    spawn_id = SpawnId("p-fast-stop")
    _publish_spawn(tmp_path, spawn_id)
    ready = tmp_path / "ready"
    grace = 5.0
    handle = await launch_managed_backend(
        ManagedBackendConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.CODEX,
            command=(
                sys.executable,
                "-c",
                f"import pathlib,time; pathlib.Path({str(ready)!r}).touch(); time.sleep(60)",
            ),
            cwd=tmp_path,
            env=dict(os.environ),
            control_root=tmp_path,
            grace_seconds=grace,
        ),
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await wait_until(ready.exists)
        start = time.monotonic()
        result = await handle.scope_handle.terminate(grace_seconds=grace)
        assert time.monotonic() - start < grace / 2
        assert not result.kill_escalated
        assert handle.process.returncode is not None
    finally:
        if handle.process.returncode is None:
            await handle.scope_handle.terminate(grace_seconds=0.1)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["backend", "stdio"])
async def test_missing_binary_uses_launch_path_and_keeps_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from meridian.lib.harness.connections.base import ConnectionConfig
    from meridian.lib.harness.connections.managed_stdio import launch_managed_stdio
    from meridian.lib.harness.errors import HarnessBinaryNotFound

    spawn_id = SpawnId("p-missing-binary")
    _publish_spawn(tmp_path, spawn_id)
    binary = tmp_path / "runner-only-binary"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    env = {"PATH": str(tmp_path / "empty-child-path")}
    with pytest.raises(HarnessBinaryNotFound) as caught:
        if mode == "backend":
            await launch_managed_backend(
                ManagedBackendConfig(
                    spawn_id=spawn_id,
                    harness_id=HarnessId.CODEX,
                    command=(binary.name,),
                    cwd=tmp_path,
                    env=env,
                    control_root=tmp_path,
                    grace_seconds=0.1,
                ),
                stderr=asyncio.subprocess.DEVNULL,
            )
        else:
            await launch_managed_stdio(
                config=ConnectionConfig(
                    spawn_id=spawn_id,
                    harness_id=HarnessId.PI,
                    prompt="",
                    control_root=tmp_path,
                    child_env=env,
                    runtime_root=resolve_project_runtime_root_for_write(tmp_path),
                ),
                harness_id=HarnessId.PI,
                command=(binary.name,),
                env=env,
                cwd=str(tmp_path),
                stdin=asyncio.subprocess.PIPE,
                stdout_limit=65536,
                kill_grace_seconds=0.1,
                terminate_reason="test",
            )
    assert caught.value.binary_name == binary.name
    assert caught.value.searched_path == env["PATH"]
