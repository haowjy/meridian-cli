"""Managed harness backend process launch helper."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import psutil

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.errors import HarnessBinaryNotFound
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.detached_process import (
    ParentDeathLink,
    ParentWatchdog,
    detached_subprocess_config,
    link_child_lifetime_to_parent,
    release_parent_death_link,
    start_parent_watchdog,
)
from meridian.lib.platform.process_scope import (
    ProcessScopeSnapshot,
    ScopedProcessHandle,
)
from meridian.lib.platform.process_scope.base import PROCESS_BIRTH_UNKNOWN_EPOCH
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
    parent_death_linked: bool = False,
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

    try:
        birth_time = psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        birth_time = PROCESS_BIRTH_UNKNOWN_EPOCH

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
        parent_death_linked=parent_death_linked,
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
    parent_watchdog: ParentWatchdog | None = None


@dataclass(frozen=True)
class ManagedBackendHandle:
    """Immutable facts about a launched managed backend."""

    process: asyncio.subprocess.Process
    scope_handle: ScopedProcessHandle
    parent_death_link: ParentDeathLink


async def register_spawn_owned_process(
    *,
    spawn_id: SpawnId,
    control_root: Path,
    process: asyncio.subprocess.Process,
    scope_id: str,
    role: str,
    parent_death_linked: bool = False,
    job_name: str | None = None,
    runtime_root: Path | None = None,
    persist: bool = True,
) -> ScopedProcessHandle:
    """Persist crash-recovery ownership for an already launched child process."""

    resolved_runtime_root = runtime_root or resolve_project_runtime_root_for_write(
        control_root
    )
    scope_handle = spawn_owned_process_handle(
        spawn_id=spawn_id,
        process=process,
        scope_id=scope_id,
        role=role,
        parent_death_linked=parent_death_linked,
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
    """Launch subprocess, build scope snapshot, link parent death, record scope.

    ``stdout`` defaults to DEVNULL. Transports that need to read a startup
    handshake from the child (OpenCode V2 prints its server password on stdout)
    pass ``asyncio.subprocess.PIPE``.
    """

    runtime_root = resolve_project_runtime_root_for_write(config.control_root)
    subprocess_config = detached_subprocess_config()
    parent_watchdog = config.parent_watchdog
    owned_watchdog: ParentWatchdog | None = None
    if not IS_WINDOWS and parent_watchdog is None:
        owned_watchdog = await asyncio.to_thread(start_parent_watchdog)
        parent_watchdog = owned_watchdog
    if not IS_WINDOWS and parent_watchdog is None:
        raise RuntimeError("Managed backend parent watchdog could not be started")

    try:
        process = await asyncio.create_subprocess_exec(
            *config.command,
            cwd=str(config.cwd),
            env=config.env,
            stdout=stdout if stdout is not None else asyncio.subprocess.DEVNULL,
            stderr=stderr,
            **subprocess_config.kwargs,
        )
    except BaseException as exc:
        if owned_watchdog is not None:
            await asyncio.to_thread(owned_watchdog.close)
        if isinstance(exc, FileNotFoundError | NotADirectoryError):
            raise HarnessBinaryNotFound.from_os_error(
                harness_id=config.harness_id,
                error=exc,
                binary_name=config.command[0],
            ) from exc
        raise

    pid = process.pid
    if IS_WINDOWS:
        parent_death_link = link_child_lifetime_to_parent(pid)
    else:
        provisional_scope = spawn_owned_process_handle(
            spawn_id=config.spawn_id,
            process=process,
            scope_id="backend",
            role="harness_backend",
        ).snapshot
        if parent_watchdog is None or not parent_watchdog.register_scope(provisional_scope):
            provisional_handle = ScopedProcessHandle(process=process, snapshot=provisional_scope)
            await provisional_handle.terminate(reason="parent_watchdog_registration_failed")
            if owned_watchdog is not None:
                await asyncio.to_thread(owned_watchdog.close)
            raise RuntimeError("Managed backend could not be linked to launcher lifetime")
        parent_death_link = ParentDeathLink(
            owned_watchdog=owned_watchdog,
            parent_death_linked=True,
        )
    parent_death_linked = parent_death_link.parent_death_linked

    try:
        scope_handle = await register_spawn_owned_process(
            spawn_id=config.spawn_id,
            control_root=config.control_root,
            process=process,
            scope_id="backend",
            role="harness_backend",
            parent_death_linked=parent_death_linked,
            job_name=parent_death_link.job_name,
            runtime_root=runtime_root,
        )
    except BaseException:
        await asyncio.to_thread(release_parent_death_link, parent_death_link)
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
