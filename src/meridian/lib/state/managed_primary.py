"""Shared managed-primary runtime helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import psutil
import structlog

from meridian.lib.core.types import SpawnId
from meridian.lib.state.liveness import is_process_alive, is_process_alive_with_birth
from meridian.lib.state.primary_meta import PrimaryMetadata, read_primary_metadata
from meridian.lib.state.reconciliation import (
    FinalizeFailed,
    FinalizeFromRunnerExit,
    ReconciliationDecision,
    Skip,
    completion_or_cancel_decision,
)
from meridian.lib.state.spawn.model import SpawnRecord

if TYPE_CHECKING:
    from meridian.lib.state.reaper import ArtifactSnapshot


logger = structlog.get_logger(__name__)


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
    ) -> ReconciliationDecision:
        """Decide reconciliation outcome for a managed primary."""

        managed = context.managed_snapshot

        if managed.launcher_pid_alive:
            return Skip(reason="primary_launcher_alive")

        if managed.metadata.activity == "finalizing" and has_recent_activity:
            return Skip(reason="recent_activity")

        decision = completion_or_cancel_decision(
            context.record,
            context.artifact_snapshot.durable_report_completion,
        )
        if decision is not None:
            return decision

        if managed.metadata.activity == "finalizing":
            return FinalizeFailed(error="orphan_finalization")
        # Launcher/backend/TUI are all dead with no durable completion. For an
        # interactive managed primary this is a terminal that ended without
        # running finalize (pane closed, process killed), not a crash-in-flight.
        # Record it as a clean stop rather than a failure, and keep the liveness
        # snapshot in the logs for diagnostics.
        logger.info(
            "Managed primary ended without finalize; finalizing as cancelled",
            spawn_id=context.record.id,
            launcher_pid=managed.metadata.launcher_pid,
            backend_pid=managed.metadata.backend_pid,
            tui_pid=managed.metadata.tui_pid,
            activity=managed.metadata.activity,
        )
        return FinalizeFromRunnerExit(
            status="cancelled",
            exit_code=130,
            error="session_ended_without_finalize",
            managed_scopes_pending=True,
        )


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
    """Conservative managed-primary identification from spawn state.

    When primary metadata is missing/corrupt we cannot prove whether a Codex or
    OpenCode primary is managed-backend or black-box, so teardown treats these
    as managed-primary candidates to avoid passive worker/TUI termination.
    """

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

    metadata = read_primary_metadata(runtime_root, str(spawn_id))
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

    if read_scopes_from_disk(runtime_root, spawn_id):
        # Phase-3 scope records: use sequenced managed-primary teardown.
        cancel_managed_primary(runtime_root, record, grace_seconds=5.0)
    else:
        # Legacy fallback: no scope records, use worker_pid termination.
        terminate_spawn_scopes(runtime_root, record, reason="cancel", grace_seconds=5.0)


__all__ = [
    "ManagedPrimaryReconciliationStrategy",
    "ManagedPrimarySnapshot",
    "ReconciliationContext",
    "is_managed_primary_candidate",
    "read_managed_primary_snapshot",
    "release_managed_primary_scopes",
    "terminate_managed_primary_processes",
]
