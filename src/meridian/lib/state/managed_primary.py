"""Shared managed-primary process teardown helpers."""

from __future__ import annotations

import os
import time
from pathlib import Path

import psutil

from meridian.lib.core.types import SpawnId
from meridian.lib.state.liveness import is_process_alive_with_birth
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.primary_meta import PrimaryMetadata, read_primary_metadata
from meridian.lib.state.spawn.model import SpawnRecord

_RELEASE_GRACE_SECS = 5.0


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


def tear_down_dead_launch(
    runtime_root: Path,
    record: SpawnRecord,
    metadata: PrimaryMetadata | None,
    *,
    until: float,
) -> None:
    """Stop what a launch with no runner left behind; the one teardown cancel uses.

    A managed primary's backend and TUI go first (by exact birth, then their recorded
    scopes), then every other recorded scope. Nothing is signalled whose birth is
    unknown. Each step's grace is the time left before ``until`` (monotonic).
    """

    # Lazy to avoid a state -> core import cycle: core.process_cleanup imports state.
    from meridian.lib.core.process_cleanup import (
        cancel_managed_primary,
        terminate_recorded_spawn_scopes,
    )

    managed = (
        metadata.managed_backend if metadata is not None else is_managed_primary_candidate(record)
    )
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
) -> None:
    """Tear down a terminal managed-primary orphan once its runner is gone."""

    from meridian.lib.state.liveness import is_process_dead_with_birth

    if not is_process_dead_with_birth(record.runner_pid, record.runner_created_at_epoch):
        return
    metadata = read_primary_metadata(LaunchPaths.for_row(runtime_root, record))
    if metadata is None and not is_managed_primary_candidate(record):
        return
    if metadata is not None and not metadata.managed_backend:
        return
    tear_down_dead_launch(
        runtime_root, record, metadata, until=time.monotonic() + _RELEASE_GRACE_SECS
    )


__all__ = [
    "is_managed_primary_candidate",
    "release_managed_primary_scopes",
    "tear_down_dead_launch",
    "terminate_managed_primary_processes",
]
