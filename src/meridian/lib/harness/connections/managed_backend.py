"""Managed harness backend process launch helper."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.errors import HarnessBinaryNotFound
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.detached_process import (
    ParentDeathLink,
    link_child_lifetime_to_parent,
)
from meridian.lib.platform.launch_gate import LaunchGate, child_birth_epoch
from meridian.lib.platform.process_scope import (
    ProcessScopeSnapshot,
    ScopedProcessHandle,
)
from meridian.lib.state.paths import (
    resolve_project_runtime_root_for_write,
)
from meridian.lib.state.process_scope_projection import record_scope


def spawn_owned_process_handle(
    *,
    spawn_id: SpawnId,
    process: asyncio.subprocess.Process,
    scope_id: str,
    role: str,
    job_name: str | None = None,
) -> ScopedProcessHandle:
    """Capture process-tree termination facts for an already launched child."""

    pid = process.pid
    pgid: int | None = None
    containment: str
    if not IS_WINDOWS:
        try:
            pgid = os.getpgid(pid)
            containment = "posix_pgid"
        except OSError:
            containment = "pid_tree_fallback"
    else:
        containment = "windows_job"

    birth_time = child_birth_epoch(pid)

    snapshot = ProcessScopeSnapshot(
        scope_id=scope_id,
        owner_policy="spawn_owned",
        owner_id=str(spawn_id),
        role=role,
        containment=containment,
        root_pid=pid,
        root_created_at_epoch=birth_time,
        pgid=pgid,
        job_name=job_name,
        degraded_reason=None,
    )
    return ScopedProcessHandle(process=process, snapshot=snapshot)


@dataclass(frozen=True)
class ManagedBackendConfig:
    """Inputs for launching a managed backend process."""

    spawn_id: SpawnId
    harness_id: HarnessId
    command: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    control_root: Path


@dataclass(frozen=True)
class ManagedBackendHandle:
    """Immutable facts about a launched managed backend."""

    process: asyncio.subprocess.Process
    scope_handle: ScopedProcessHandle
    parent_death_link: ParentDeathLink | None = None


async def register_spawn_owned_process(
    *,
    spawn_id: SpawnId,
    control_root: Path,
    process: asyncio.subprocess.Process,
    scope_id: str,
    role: str,
    job_name: str | None = None,
    runtime_root: Path | None = None,
    persist: bool = True,
) -> ScopedProcessHandle:
    """Persist crash-recovery ownership for an already launched child process."""

    resolved_runtime_root = runtime_root or resolve_project_runtime_root_for_write(control_root)
    scope_handle = spawn_owned_process_handle(
        spawn_id=spawn_id,
        process=process,
        scope_id=scope_id,
        role=role,
        job_name=job_name,
    )
    try:
        if persist:
            record_scope(resolved_runtime_root, spawn_id, scope_handle.snapshot)
    except BaseException:
        await scope_handle.terminate(reason="scope_registration_failed")
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
    gate = None if IS_WINDOWS else LaunchGate()
    process: asyncio.subprocess.Process | None = None
    try:
        try:
            process = await asyncio.create_subprocess_exec(
                *(gate.command(config.command) if gate is not None else config.command),
                cwd=str(config.cwd),
                env=gate.environment(config.env) if gate is not None else config.env,
                stdout=stdout if stdout is not None else asyncio.subprocess.DEVNULL,
                stderr=stderr,
                start_new_session=not IS_WINDOWS,
                pass_fds=(gate.read_fd,) if gate is not None else (),
            )
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise HarnessBinaryNotFound.from_os_error(
                harness_id=config.harness_id,
                error=exc,
                binary_name=config.command[0],
            ) from exc
        if gate is not None:
            gate.close_read_end()
        parent_death_link = link_child_lifetime_to_parent(process.pid) if IS_WINDOWS else None
        scope_handle = await register_spawn_owned_process(
            spawn_id=config.spawn_id,
            control_root=config.control_root,
            process=process,
            scope_id="backend",
            role="harness_backend",
            job_name=parent_death_link.job_name if parent_death_link is not None else None,
            runtime_root=runtime_root,
        )
        if gate is not None:
            gate.open()
            gate.watch_exit(process)
    except BaseException:
        if gate is not None:
            gate.close()
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        raise

    return ManagedBackendHandle(
        process=process,
        scope_handle=scope_handle,
        parent_death_link=parent_death_link,
    )


__all__ = [
    "ManagedBackendConfig",
    "ManagedBackendHandle",
    "launch_managed_backend",
    "register_spawn_owned_process",
]
