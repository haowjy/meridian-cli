"""Shared managed-primary process teardown helpers."""

from __future__ import annotations

import os
from pathlib import Path

import psutil

from meridian.lib.core.types import SpawnId
from meridian.lib.state.liveness import is_process_alive_with_birth
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.primary_meta import PrimaryMetadata, read_primary_metadata
from meridian.lib.state.spawn.model import SpawnRecord


def _terminate_pid(pid: int) -> bool:
    """Terminate a single process by PID using psutil."""

    if pid <= 0 or pid == os.getpid():
        return False
    try:
        psutil.Process(pid).terminate()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False
    return True


def terminate_managed_primary_processes(
    primary_metadata: PrimaryMetadata | None,
    *,
    include_launcher: bool,
    include_runtime_children: bool = True,
) -> tuple[int, ...]:
    """Best-effort SIGTERM for exact-birth-validated managed-primary processes."""

    if primary_metadata is None or not primary_metadata.managed_backend:
        return ()

    candidates: list[tuple[int | None, float | None]] = []
    if include_launcher:
        candidates.append((primary_metadata.launcher_pid, primary_metadata.launcher_birth_epoch))
    if include_runtime_children:
        candidates.extend(
            (
                (primary_metadata.backend_pid, primary_metadata.backend_birth_epoch),
                (primary_metadata.tui_pid, primary_metadata.tui_birth_epoch),
            )
        )

    signaled: list[int] = []
    seen: set[int] = set()
    for candidate, birth_epoch in candidates:
        if candidate is None or candidate in seen:
            continue
        seen.add(candidate)
        if not is_process_alive_with_birth(candidate, birth_epoch):
            continue
        if _terminate_pid(candidate):
            signaled.append(candidate)
    return tuple(signaled)


def is_managed_primary_candidate(record: SpawnRecord) -> bool:
    """Conservatively identify managed-primary rows when metadata is unavailable."""

    harness = (record.harness or "").strip().lower()
    return record.kind == "primary" and harness in {"codex", "opencode"}


def release_managed_primary_scopes(
    runtime_root: Path,
    spawn_id: SpawnId,
    record: SpawnRecord,
) -> None:
    """Best-effort teardown of a terminal managed-primary orphan's processes.

    Single owner of the cancel-on-terminal release path. Prefers managed-primary
    metadata PIDs, then recorded process scopes, then the legacy worker fallback.
    """

    from meridian.lib.state.liveness import is_process_dead_with_birth

    if not is_process_dead_with_birth(record.runner_pid, record.runner_created_at_epoch):
        return

    launch = LaunchPaths.for_row(runtime_root, record)
    metadata = read_primary_metadata(launch)
    if metadata is not None:
        if metadata.managed_backend:
            terminate_managed_primary_processes(metadata, include_launcher=False)
        return

    if not is_managed_primary_candidate(record):
        return

    # Lazy to avoid a state -> core import cycle: core.process_cleanup imports state.
    from meridian.lib.core.process_cleanup import (
        cancel_managed_primary,
        terminate_spawn_scopes,
    )
    from meridian.lib.state.process_scope_projection import read_scopes_from_disk

    if read_scopes_from_disk(launch):
        # Phase-3 scope records: use sequenced managed-primary teardown.
        cancel_managed_primary(runtime_root, record, grace_seconds=5.0)
    else:
        # Legacy fallback: no scope records, use worker_pid termination.
        terminate_spawn_scopes(runtime_root, record, reason="cancel", grace_seconds=5.0)


__all__ = [
    "is_managed_primary_candidate",
    "release_managed_primary_scopes",
    "terminate_managed_primary_processes",
]
