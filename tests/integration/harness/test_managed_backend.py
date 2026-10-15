"""Managed backend launch helper tests."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections import managed_backend
from meridian.lib.harness.connections.managed_backend import (
    ManagedBackendConfig,
    launch_managed_backend,
)
from meridian.lib.platform.detached_process import (
    ParentDeathLink,
    release_parent_death_link,
)
from meridian.lib.platform.process_scope import ScopedProcessHandle
from meridian.lib.platform.process_scope.base import PROCESS_BIRTH_UNKNOWN_EPOCH
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
async def test_launch_managed_backend_records_scope_with_parent_death_outcome(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("spawn-3")
    _publish_spawn(tmp_path, spawn_id)
    registered = []
    released: list[str] = []

    class RecordingWatchdog:
        def register_scope(self, scope: object) -> bool:
            registered.append(scope)
            return True

        def release_scope(self, release_id: str) -> bool:
            released.append(release_id)
            return True

    handle = await launch_managed_backend(
        ManagedBackendConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.CODEX,
            command=(sys.executable, "-c", "pass"),
            cwd=tmp_path,
            env=os.environ.copy(),
            control_root=tmp_path,
            parent_watchdog=RecordingWatchdog(),  # type: ignore[arg-type]
        ),
        stderr=asyncio.subprocess.DEVNULL,
    )

    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    scopes = read_scopes_from_disk(runtime_root, spawn_id)

    assert handle.scope_handle.snapshot.parent_death_linked is True
    assert len(registered) == 1
    assert [(scope.scope_id, scope.root_pid, scope.parent_death_linked) for scope in scopes] == [
        ("backend", handle.scope_handle.snapshot.root_pid, True)
    ]
    await handle.process.wait()
    await asyncio.to_thread(release_parent_death_link, handle.parent_death_link)
    assert released == [handle.scope_handle.snapshot.release_id]


@pytest.mark.asyncio
async def test_launch_managed_backend_refuses_unknown_birth_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_id = SpawnId("spawn-unknown-birth")
    _publish_spawn(tmp_path, spawn_id)

    class RejectUnknownBirthWatchdog:
        def register_scope(self, scope: object) -> bool:
            snapshot = scope
            assert snapshot.root_created_at_epoch == PROCESS_BIRTH_UNKNOWN_EPOCH
            return False

    original_spawn_owned_process_handle = managed_backend.spawn_owned_process_handle

    def _unknown_birth_handle(**kwargs: object) -> ScopedProcessHandle:
        handle = original_spawn_owned_process_handle(**kwargs)  # type: ignore[arg-type]
        return ScopedProcessHandle(
            process=handle.process,
            snapshot=replace(
                handle.snapshot,
                root_created_at_epoch=PROCESS_BIRTH_UNKNOWN_EPOCH,
                release_id="",
            ),
        )

    monkeypatch.setattr(
        managed_backend,
        "spawn_owned_process_handle",
        _unknown_birth_handle,
    )
    with pytest.raises(RuntimeError, match="could not be linked"):
        await launch_managed_backend(
            ManagedBackendConfig(
                spawn_id=spawn_id,
                harness_id=HarnessId.OPENCODE,
                command=(sys.executable, "-c", "import time; time.sleep(60)"),
                cwd=tmp_path,
                env=os.environ.copy(),
                control_root=tmp_path,
                parent_watchdog=RejectUnknownBirthWatchdog(),  # type: ignore[arg-type]
            ),
            stderr=asyncio.subprocess.DEVNULL,
        )

    assert read_scopes_from_disk(
        resolve_project_runtime_root_for_write(tmp_path), spawn_id
    ) == []


@pytest.mark.asyncio
async def test_launch_managed_backend_cleans_up_when_terminal_spawn_rejects_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_id = SpawnId("terminal-spawn")
    _publish_spawn(tmp_path, spawn_id, status="succeeded")
    launched: list[asyncio.subprocess.Process] = []
    released: list[ParentDeathLink | None] = []
    original_launch = asyncio.create_subprocess_exec

    async def capture_launch(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        process = await original_launch(*args, **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(managed_backend.asyncio, "create_subprocess_exec", capture_launch)
    monkeypatch.setattr(
        managed_backend,
        "release_parent_death_link",
        lambda link: released.append(link),
        raising=False,
    )

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
                ),
                stderr=asyncio.subprocess.DEVNULL,
            )

        assert len(launched) == 1
        # psutil can observe exit before asyncio's child watcher sets returncode.
        await asyncio.wait_for(launched[0].wait(), timeout=1.0)
        assert launched[0].returncode is not None
        assert len(released) == 1
    finally:
        for process in launched:
            if process.returncode is None:
                process.kill()
                await process.wait()
