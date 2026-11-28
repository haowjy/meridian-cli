from __future__ import annotations

import os
import time
from pathlib import Path

import psutil

from meridian.lib.core.types import SpawnId
from meridian.lib.spawn.cleanup import (
    cancel_managed_primary,
    terminate_recorded_spawn_scopes,
)
from meridian.lib.state.liveness import is_process_alive_with_birth, is_process_dead_with_birth
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.primary_meta import PrimaryMetadata, read_primary_metadata
from meridian.lib.state.spawn.model import SpawnRecord

_RELEASE_GRACE_SECS = 5.0


TEARDOWN_GRACE_SECS = 5.0


def _terminate_pid(pid: int) -> bool:

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


def tear_down_dead_launch(
    runtime_root: Path,
    record: SpawnRecord,
    metadata: PrimaryMetadata | None,
    *,
    managed_if_unrecorded: bool,
    until: float,
) -> None:
    """Stop what a launch with no runner left behind; the one teardown cancel uses.

    ``managed_if_unrecorded`` says whether the row's harness runs managed primaries; it
    decides only when no launch metadata survives.

    A managed primary's backend and TUI go first (by exact birth, then their recorded
    scopes), then every other recorded scope. Nothing is signalled whose birth is
    unknown. Each step's grace is the time left before ``until`` (monotonic).
    """

    managed = metadata.managed_backend if metadata is not None else managed_if_unrecorded
    if managed:
        terminate_managed_primary_processes(metadata, include_launcher=False)
        cancel_managed_primary(
            runtime_root, record, grace_seconds=max(0.0, until - time.monotonic())
        )
    terminate_recorded_spawn_scopes(
        runtime_root, record, reason="cancel", grace_seconds=max(0.0, until - time.monotonic())
    )


def release_managed_primary_scopes(
    runtime_root: Path,
    spawn_id: SpawnId,
    record: SpawnRecord,
    *,
    managed_if_unrecorded: bool,
) -> None:
    """Tear down a terminal managed-primary orphan once its runner is gone."""

    if not is_process_dead_with_birth(record.runner_pid, record.runner_created_at_epoch):
        return
    metadata = read_primary_metadata(LaunchPaths.for_row(runtime_root, record))
    if metadata is None and not managed_if_unrecorded:
        return
    if metadata is not None and not metadata.managed_backend:
        return
    tear_down_dead_launch(
        runtime_root,
        record,
        metadata,
        managed_if_unrecorded=managed_if_unrecorded,
        until=time.monotonic() + _RELEASE_GRACE_SECS,
    )


def require_managed_processes_gone(metadata: PrimaryMetadata | None, spawn_id: str) -> None:
    """The backend and TUI were signalled by exact birth; the next launch needs them gone."""
    if metadata is None or not metadata.managed_backend:
        return
    survivors = (
        (metadata.backend_pid, metadata.backend_birth_epoch),
        (metadata.tui_pid, metadata.tui_birth_epoch),
    )
    deadline = time.monotonic() + TEARDOWN_GRACE_SECS
    while time.monotonic() < deadline:
        if not any(
            pid is not None and is_process_alive_with_birth(pid, birth) for pid, birth in survivors
        ):
            return
        time.sleep(0.05)
    raise ValueError(
        f"{spawn_id} has processes from its last run that could not be stopped; "
        "run `meridian doctor`"
    )


__all__ = [
    "release_managed_primary_scopes",
    "require_managed_processes_gone",
    "tear_down_dead_launch",
    "terminate_managed_primary_processes",
]
