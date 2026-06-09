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
    stderr_log_path: Path
    observer_mode: bool


@dataclass(frozen=True)
class ManagedBackendHandle:
    """Immutable facts about a launched managed backend."""

    process: asyncio.subprocess.Process
    pid: int
    scope_snapshot: ProcessScopeSnapshot
    scope_handle: ScopedProcessHandle
    parent_death_link: ParentDeathLink
    parent_death_linked: bool


async def launch_managed_backend(
    config: ManagedBackendConfig,
    *,
    stderr: int | IO[Any],
) -> ManagedBackendHandle:
    """Launch subprocess, build scope snapshot, link parent death, record scope."""

    spawn_dir = resolve_spawn_log_dir(config.control_root, config.spawn_id)
    spawn_dir.mkdir(parents=True, exist_ok=True)
    runtime_root = spawn_dir.parent.parent
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

    parent_death_link = link_child_lifetime_to_parent(pid)
    parent_death_linked = (
        subprocess_config.parent_death_linked or parent_death_link.parent_death_linked
    )

    snapshot = ProcessScopeSnapshot(
        scope_id="backend",
        owner_policy="spawn_owned",
        owner_id=str(config.spawn_id),
        role="harness_backend",
        containment=containment,
        root_pid=pid,
        root_created_at_epoch=birth_time,
        pgid=pgid,
        job_name=parent_death_link.job_name,
        degraded_reason=None,
        parent_death_linked=parent_death_linked,
    )
    scope_handle = ScopedProcessHandle(process=process, snapshot=snapshot)

    record_scope(runtime_root, config.spawn_id, snapshot)

    return ManagedBackendHandle(
        process=process,
        pid=pid,
        scope_snapshot=snapshot,
        scope_handle=scope_handle,
        parent_death_link=parent_death_link,
        parent_death_linked=parent_death_linked,
    )


__all__ = [
    "ManagedBackendConfig",
    "ManagedBackendHandle",
    "launch_managed_backend",
]
