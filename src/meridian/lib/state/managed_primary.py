"""Shared managed-primary runtime helpers."""

from __future__ import annotations

import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.state.liveness import is_process_alive
from meridian.lib.state.primary_meta import PrimaryMetadata, read_primary_metadata
from meridian.lib.state.spawn.model import SpawnRecord

if TYPE_CHECKING:
    from meridian.lib.state.reaper import ArtifactSnapshot, ReconciliationDecision


@dataclass(frozen=True)
class ManagedPrimarySnapshot:
    """Snapshot of managed primary runtime state for reconciliation."""

    metadata: PrimaryMetadata
    launcher_pid_alive: bool
    started_epoch: float | None


@dataclass(frozen=True)
class ReconciliationContext:
    """Context passed to reconciliation strategies."""

    record: SpawnRecord
    artifact_snapshot: ArtifactSnapshot
    managed_snapshot: ManagedPrimarySnapshot
    now: float


class ManagedPrimaryReconciliationStrategy:
    """Managed-primary reconciliation policy."""

    @staticmethod
    def supports(snapshot: ManagedPrimarySnapshot | None) -> bool:
        """Return whether this strategy handles the given snapshot."""

        return snapshot is not None and snapshot.metadata.managed_backend

    @staticmethod
    def decide(
        context: ReconciliationContext,
        *,
        has_recent_activity: bool,
        durable_report_completion: bool,
    ) -> ReconciliationDecision:
        """Decide reconciliation outcome for a managed primary."""

        # Import here to avoid circular dependency.
        from meridian.lib.state.reaper import (
            FinalizeFailed,
            FinalizeSucceededFromReport,
            Skip,
        )

        managed = context.managed_snapshot

        if managed.launcher_pid_alive:
            return Skip(reason="primary_launcher_alive")

        if managed.metadata.activity == "finalizing":
            if has_recent_activity:
                return Skip(reason="recent_activity")
            if durable_report_completion:
                return FinalizeSucceededFromReport()
            return FinalizeFailed(error="orphan_finalization")

        return FinalizeFailed(error="orphan_primary")


def read_managed_primary_snapshot(
    runtime_root: Path,
    record: SpawnRecord,
    *,
    started_epoch: float | None = None,
) -> ManagedPrimarySnapshot | None:
    """Read managed primary snapshot for reconciliation decisions."""

    metadata = read_primary_metadata(runtime_root, record.id)
    if metadata is None or not metadata.managed_backend:
        return None

    launcher_pid_alive = False
    if metadata.launcher_pid is not None:
        launcher_pid_alive = is_process_alive(
            metadata.launcher_pid,
            created_after_epoch=started_epoch,
        )

    return ManagedPrimarySnapshot(
        metadata=metadata,
        launcher_pid_alive=launcher_pid_alive,
        started_epoch=started_epoch,
    )


def _terminate_pid(pid: int) -> bool:
    """Send SIGTERM to a single PID."""

    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return False
    return True


def terminate_managed_primary_processes(
    primary_metadata: PrimaryMetadata | None,
    *,
    started_epoch: float | None = None,
    include_launcher: bool,
    include_runtime_children: bool = True,
) -> tuple[int, ...]:
    """Best-effort SIGTERM for tracked managed-primary processes."""

    if primary_metadata is None or not primary_metadata.managed_backend:
        return ()

    if include_launcher and include_runtime_children:
        candidates = (
            primary_metadata.launcher_pid,
            primary_metadata.backend_pid,
            primary_metadata.tui_pid,
        )
    elif include_launcher:
        candidates = (primary_metadata.launcher_pid,)
    else:
        candidates = (
            primary_metadata.backend_pid,
            primary_metadata.tui_pid,
        )

    signaled: list[int] = []
    seen: set[int] = set()
    for candidate in candidates:
        if candidate is None or candidate in seen:
            continue
        seen.add(candidate)
        if not is_process_alive(candidate, created_after_epoch=started_epoch):
            continue
        if _terminate_pid(candidate):
            signaled.append(candidate)
    return tuple(signaled)


__all__ = [
    "ManagedPrimaryReconciliationStrategy",
    "ManagedPrimarySnapshot",
    "ReconciliationContext",
    "read_managed_primary_snapshot",
    "terminate_managed_primary_processes",
]
