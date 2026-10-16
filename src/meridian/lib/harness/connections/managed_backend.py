"""Managed harness backend process launch helper."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import reap_on_ownership_transfer_failure
from meridian.lib.harness.errors import HarnessBinaryNotFound
from meridian.lib.platform.launch_gate import LaunchGate, ProcessIdentity
from meridian.lib.platform.process_scope import (
    ScopedProcessHandle,
    scope_from_identity,
)
from meridian.lib.state.paths import (
    resolve_project_runtime_root_for_write,
)
from meridian.lib.state.process_scope_projection import record_scope


@dataclass(frozen=True)
class ManagedBackendConfig:
    """Inputs for launching a managed backend process."""

    spawn_id: SpawnId
    harness_id: HarnessId
    command: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    control_root: Path
    grace_seconds: float


@dataclass(frozen=True)
class ManagedBackendHandle:
    """Immutable facts about a launched managed backend."""

    process: asyncio.subprocess.Process
    scope_handle: ScopedProcessHandle


async def kill_and_reap_unowned_process(process: asyncio.subprocess.Process) -> None:
    """Abort startup without taking reaping away from asyncio's child watcher."""
    with suppress(ProcessLookupError):
        process.kill()
    await reap_on_ownership_transfer_failure(process.wait)


async def register_spawn_owned_process(
    *,
    spawn_id: SpawnId,
    control_root: Path,
    process: asyncio.subprocess.Process,
    scope_id: str,
    role: str,
    identity: ProcessIdentity,
    gate: LaunchGate,
    runtime_root: Path | None = None,
    persist: bool = True,
) -> ScopedProcessHandle:
    """Persist crash-recovery ownership for an already launched child process."""

    resolved_runtime_root = runtime_root or resolve_project_runtime_root_for_write(control_root)
    scope_handle = ScopedProcessHandle(
        process=process,
        snapshot=scope_from_identity(
            identity,
            scope_id=scope_id,
            role=role,
            owner_policy="spawn_owned",
            owner_id=str(spawn_id),
        ),
        gate=gate,
    )
    try:
        if persist:
            record_scope(resolved_runtime_root, spawn_id, scope_handle.snapshot)
    except BaseException:
        await reap_on_ownership_transfer_failure(
            lambda: scope_handle.terminate(reason="scope_registration_failed")
        )
        raise
    return scope_handle


async def launch_managed_backend(
    config: ManagedBackendConfig,
    *,
    stderr: int | IO[Any],
    stdout: int | IO[Any] | None = None,
) -> ManagedBackendHandle:
    """Launch a blocked stub, record ownership, then release the backend.

    ``stdout`` defaults to DEVNULL. Transports that need to read a startup
    handshake from the child (OpenCode V2 prints its server password on stdout)
    pass ``asyncio.subprocess.PIPE``.
    """

    runtime_root = resolve_project_runtime_root_for_write(config.control_root)
    gate = LaunchGate(grace_seconds=config.grace_seconds)
    process: asyncio.subprocess.Process | None = None
    try:
        try:
            launch = gate.spawn_kwargs(config.command, config.env, cwd=config.cwd)
            process = await asyncio.create_subprocess_exec(
                *launch["args"],
                cwd=str(config.cwd),
                env=launch["env"],
                stdout=stdout if stdout is not None else asyncio.subprocess.DEVNULL,
                stderr=stderr,
                start_new_session=launch["start_new_session"],
                pass_fds=launch["pass_fds"],
            )
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise HarnessBinaryNotFound.from_os_error(
                harness_id=config.harness_id,
                error=exc,
                binary_name=config.command[0],
                searched_path=os.pathsep.join(os.get_exec_path(config.env)),
            ) from exc
        identity = await gate.adopt_async(
            process.pid, kill_and_reap=lambda: kill_and_reap_unowned_process(process)
        )
        scope_handle = await register_spawn_owned_process(
            spawn_id=config.spawn_id,
            control_root=config.control_root,
            process=process,
            scope_id="backend",
            role="harness_backend",
            identity=identity,
            gate=gate,
            runtime_root=runtime_root,
        )
        gate.open()
    except BaseException:
        gate.close()
        if process is not None and process.returncode is None:
            await kill_and_reap_unowned_process(process)
        raise

    return ManagedBackendHandle(
        process=process,
        scope_handle=scope_handle,
    )


__all__ = [
    "ManagedBackendConfig",
    "ManagedBackendHandle",
    "launch_managed_backend",
    "register_spawn_owned_process",
]
