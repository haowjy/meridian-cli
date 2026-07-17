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
    detached_subprocess_config,
    link_child_lifetime_to_parent,
    release_parent_death_link,
)
from meridian.lib.platform.process_scope import (
    ProcessScopeSnapshot,
    ScopedProcessHandle,
)
from meridian.lib.platform.process_scope.base import PROCESS_BIRTH_UNKNOWN_EPOCH
from meridian.lib.state.paths import resolve_spawn_log_dir
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

    spawn_dir = resolve_spawn_log_dir(control_root, spawn_id)
    spawn_dir.mkdir(parents=True, exist_ok=True)
    resolved_runtime_root = runtime_root or spawn_dir.parent.parent
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
    scope_handle = ScopedProcessHandle(process=process, snapshot=snapshot)
    try:
        if persist:
            record_scope(resolved_runtime_root, spawn_id, snapshot)
    except BaseException:
        await scope_handle.terminate(reason="scope_registration_failed")
        raise
    return scope_handle

async def launch_managed_backend(
    config: ManagedBackendConfig,
    *,
    stderr: int | IO[Any],
) -> ManagedBackendHandle:
    """Launch subprocess, build scope snapshot, link parent death, record scope."""

    spawn_dir = resolve_spawn_log_dir(config.control_root, config.spawn_id)
    spawn_dir.mkdir(parents=True, exist_ok=True)
    subprocess_config = detached_subprocess_config()

    try:
        process = await asyncio.create_subprocess_exec(
            *config.command,
            cwd=str(config.cwd),
            env=config.env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=stderr,
            **subprocess_config.kwargs,
        )
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise HarnessBinaryNotFound.from_os_error(
            harness_id=config.harness_id,
            error=exc,
            binary_name=config.command[0],
        ) from exc

    pid = process.pid
    parent_death_link = (
        ParentDeathLink(parent_death_linked=False)
        if subprocess_config.parent_death_linked
        else link_child_lifetime_to_parent(pid)
    )
    parent_death_linked = (
        subprocess_config.parent_death_linked or parent_death_link.parent_death_linked
    )

    try:
        scope_handle = await register_spawn_owned_process(
            spawn_id=config.spawn_id,
            control_root=config.control_root,
            process=process,
            scope_id="backend",
            role="harness_backend",
            parent_death_linked=parent_death_linked,
            job_name=parent_death_link.job_name,
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
