"""Spawn reconciliation: detect orphaned spawns via process liveness."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog

from meridian.lib.core.depth import is_root_side_effect_process
from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.lifecycle import create_lifecycle_service
from meridian.lib.core.spawn_lifecycle import (
    has_durable_report_completion,
    is_active_spawn_status,
    resolve_reconciled_terminal_state,
)
from meridian.lib.core.spawn_service import SpawnApplicationService
from meridian.lib.core.types import SpawnId
from meridian.lib.launch.constants import HISTORY_FILENAME, OUTPUT_FILENAME
from meridian.lib.state.liveness import is_process_alive
from meridian.lib.state.managed_primary import (
    ManagedPrimaryReconciliationStrategy,
    ManagedPrimarySnapshot,
    ReconciliationContext,
    read_managed_primary_snapshot,
)
from meridian.lib.state.spawn.model import SpawnRecord

logger = structlog.get_logger(__name__)

_STARTUP_GRACE_SECS = 15
_HEARTBEAT_WINDOW_SECS = 120
_ACTIVITY_ARTIFACTS: tuple[str, ...] = (
    "heartbeat",
    HISTORY_FILENAME,
    OUTPUT_FILENAME,
    "stderr.log",
    "report.md",
)


@dataclass(frozen=True)
class ArtifactSnapshot:
    started_epoch: float | None
    last_activity_epoch: float | None
    recent_activity_artifact: str | None
    durable_report_completion: bool
    runner_pid_alive: bool


@dataclass(frozen=True)
class Skip:
    reason: str


@dataclass(frozen=True)
class FinalizeFailed:
    error: str


@dataclass(frozen=True)
class FinalizeSucceededFromReport:
    pass


type ReconciliationDecision = Skip | FinalizeFailed | FinalizeSucceededFromReport


def _started_at_epoch(started_at: str | None) -> float | None:
    """Parse started_at ISO string to epoch seconds."""
    normalized = (started_at or "").strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _read_completion_report(runtime_root: Path, spawn_id: str) -> str | None:
    """Read report.md for durable completion check."""
    report_path = runtime_root / "spawns" / spawn_id / "report.md"
    if not report_path.is_file():
        return None
    try:
        return report_path.read_text(encoding="utf-8", errors="ignore").strip() or None
    except OSError:
        return None


def _artifact_mtime_epoch(path: Path) -> float | None:
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return stat_result.st_mtime


def _recent_runner_activity(
    runtime_root: Path, spawn_id: str, now: float
) -> tuple[float | None, str | None]:
    """Return the freshest activity timestamp and the artifact that proved recency."""
    spawn_dir = runtime_root / "spawns" / spawn_id
    latest_activity_epoch: float | None = None
    for artifact_name in _ACTIVITY_ARTIFACTS:
        mtime_epoch = _artifact_mtime_epoch(spawn_dir / artifact_name)
        if mtime_epoch is None:
            continue
        if latest_activity_epoch is None or mtime_epoch > latest_activity_epoch:
            latest_activity_epoch = mtime_epoch
        if now - mtime_epoch <= _HEARTBEAT_WINDOW_SECS:
            return mtime_epoch, artifact_name
    return latest_activity_epoch, None


def _collect_artifact_snapshot(
    runtime_root: Path,
    record: SpawnRecord,
    now: float,
) -> ArtifactSnapshot:
    started_epoch = _started_at_epoch(record.started_at)
    last_activity_epoch, recent_activity_artifact = _recent_runner_activity(
        runtime_root,
        record.id,
        now,
    )
    report_text = _read_completion_report(runtime_root, record.id)
    runner_pid_alive = False
    if record.status != "finalizing" and record.runner_pid is not None and record.runner_pid > 0:
        runner_pid_alive = is_process_alive(
            record.runner_pid,
            created_after_epoch=started_epoch,
        )
    return ArtifactSnapshot(
        started_epoch=started_epoch,
        last_activity_epoch=last_activity_epoch,
        recent_activity_artifact=recent_activity_artifact,
        durable_report_completion=has_durable_report_completion(report_text),
        runner_pid_alive=runner_pid_alive,
    )


def _terminate_worker_pid(worker_pid: int, started_epoch: float | None) -> None:
    """Terminate a leaked harness subprocess during orphan reconciliation.

    Uses PID-reuse guard to avoid killing unrelated processes.
    Suppresses all errors -- best-effort cleanup in a crash-recovery path.
    """
    try:
        created_after = started_epoch if started_epoch is not None else 0.0
        if not is_process_alive(worker_pid, created_after_epoch=created_after):
            return
        os.kill(worker_pid, signal.SIGTERM)
    except OSError:
        pass


def _has_recent_activity(snapshot: ArtifactSnapshot) -> bool:
    """Return whether any tracked runner artifact is recent."""
    return snapshot.recent_activity_artifact is not None


def _is_potential_managed_primary(record: SpawnRecord) -> bool:
    """Conservative managed-primary fallback identification from spawn state.

    When primary metadata is missing/corrupt we cannot prove whether a Codex or
    OpenCode primary is managed-backend or black-box, so reconciliation treats
    these as managed-primary candidates to avoid passive worker/TUI termination.
    """

    harness = (record.harness or "").strip().lower()
    return record.kind == "primary" and harness in {"codex", "opencode"}


def decide_generic_reconciliation(
    record: SpawnRecord,
    snapshot: ArtifactSnapshot,
    now: float,
) -> ReconciliationDecision:
    if record.status == "finalizing":
        if _has_recent_activity(snapshot):
            return Skip(reason="recent_activity")
        if snapshot.durable_report_completion:
            return FinalizeSucceededFromReport()
        return FinalizeFailed(error="orphan_finalization")

    runner_pid = record.runner_pid
    if runner_pid is None or runner_pid <= 0:
        if _has_recent_activity(snapshot):
            return Skip(reason="recent_activity")
        if _in_startup_grace(snapshot.started_epoch, now):
            return Skip(reason="startup_grace")
        if snapshot.durable_report_completion:
            return FinalizeSucceededFromReport()
        return FinalizeFailed(error="missing_runner_pid")

    if snapshot.runner_pid_alive:
        if _has_recent_activity(snapshot):
            return Skip(reason="recent_activity")
        return Skip(reason="runner_alive")

    if _has_recent_activity(snapshot):
        return Skip(reason="recent_activity")

    if _in_startup_grace(snapshot.started_epoch, now):
        return Skip(reason="startup_grace")
    if snapshot.durable_report_completion:
        return FinalizeSucceededFromReport()
    return FinalizeFailed(error="orphan_run")


def decide_reconciliation(
    record: SpawnRecord,
    generic_snapshot: ArtifactSnapshot,
    managed_snapshot: ManagedPrimarySnapshot | None,
    now: float,
) -> ReconciliationDecision:
    """Unified reconciliation dispatcher."""

    strategy = ManagedPrimaryReconciliationStrategy()
    if strategy.supports(managed_snapshot):
        assert managed_snapshot is not None
        context = ReconciliationContext(
            record=record,
            artifact_snapshot=generic_snapshot,
            managed_snapshot=managed_snapshot,
            now=now,
        )
        return strategy.decide(
            context,
            has_recent_activity=_has_recent_activity(generic_snapshot),
            durable_report_completion=generic_snapshot.durable_report_completion,
        )

    generic_decision = decide_generic_reconciliation(record, generic_snapshot, now)
    if (
        _is_potential_managed_primary(record)
        and isinstance(generic_decision, FinalizeFailed)
        and generic_decision.error in {"missing_runner_pid", "orphan_run"}
    ):
        return FinalizeFailed(error="orphan_primary")
    return generic_decision


def _log_orphan_primary_diagnostics(
    record: SpawnRecord,
    snapshot: ArtifactSnapshot,
    managed_snapshot: ManagedPrimarySnapshot | None,
) -> None:
    if managed_snapshot is not None:
        metadata = managed_snapshot.metadata
        launcher_pid = metadata.launcher_pid
        launcher_alive = (
            managed_snapshot.launcher_pid_alive if launcher_pid is not None else None
        )
        backend_alive = (
            is_process_alive(
                metadata.backend_pid,
                created_after_epoch=managed_snapshot.started_epoch,
            )
            if metadata.backend_pid is not None
            else None
        )
        tui_alive = (
            is_process_alive(
                metadata.tui_pid,
                created_after_epoch=managed_snapshot.started_epoch,
            )
            if metadata.tui_pid is not None
            else None
        )
        logger.warning(
            "Managed primary launcher dead; reconciler did not terminate tracked children.",
            spawn_id=record.id,
            managed_metadata_readable=True,
            launcher_pid=launcher_pid,
            launcher_alive=launcher_alive,
            backend_pid=metadata.backend_pid,
            backend_alive=backend_alive,
            tui_pid=metadata.tui_pid,
            tui_alive=tui_alive,
            activity=metadata.activity,
        )
        return

    launcher_pid = (
        record.runner_pid
        if record.runner_pid is not None and record.runner_pid > 0
        else None
    )
    launcher_alive = snapshot.runner_pid_alive if launcher_pid is not None else None
    logger.warning(
        (
            "Managed primary candidate reconciled without readable metadata; "
            "skipped worker termination."
        ),
        spawn_id=record.id,
        managed_metadata_readable=False,
        launcher_pid=launcher_pid,
        launcher_alive=launcher_alive,
        backend_pid=None,
        backend_alive=None,
        tui_pid=None,
        tui_alive=None,
        activity=None,
    )


def _finalize_and_log(
    runtime_root: Path, record: SpawnRecord, *, status: SpawnStatus, exit_code: int,
    error: str | None, reason: str, snapshot: ArtifactSnapshot, now: float
) -> SpawnRecord:
    lifecycle_service = create_lifecycle_service(runtime_root.parent, runtime_root)
    outcome = asyncio.run(
        SpawnApplicationService(runtime_root, lifecycle_service).complete_spawn(
            SpawnId(record.id),
            status,
            exit_code,
            origin="reconciler",
            error=error,
        )
    )
    if not outcome.wrote:
        return record
    inactivity_secs = (
        max(0.0, now - snapshot.last_activity_epoch)
        if snapshot.last_activity_epoch is not None
        else None
    )
    logger.info(
        "Reconciled active spawn.",
        spawn_id=record.id,
        status=status,
        reason=reason,
        heartbeat_window_secs=_HEARTBEAT_WINDOW_SECS,
        last_activity_epoch=snapshot.last_activity_epoch,
        recent_activity_artifact=snapshot.recent_activity_artifact,
        inactivity_secs=inactivity_secs,
    )
    return record.model_copy(update={"status": status, "exit_code": exit_code, "error": error})


def _finalize_failed(
    runtime_root: Path, record: SpawnRecord, error: str, snapshot: ArtifactSnapshot, now: float
) -> SpawnRecord:
    return _finalize_and_log(
        runtime_root,
        record,
        status="failed",
        exit_code=1,
        error=error,
        reason=error,
        snapshot=snapshot,
        now=now,
    )


def _finalize_completed_report(
    runtime_root: Path, record: SpawnRecord, snapshot: ArtifactSnapshot, now: float
) -> SpawnRecord:
    status, exit_code, error = resolve_reconciled_terminal_state(
        durable_report_completion=True,
        fallback_error="harness_completed",
    )
    return _finalize_and_log(
        runtime_root,
        record,
        status=status,
        exit_code=exit_code,
        error=error,
        reason="report_completed",
        snapshot=snapshot,
        now=now,
    )


def _in_startup_grace(started_epoch: float | None, now: float) -> bool:
    return started_epoch is not None and now - started_epoch < _STARTUP_GRACE_SECS


def reconcile_active_spawn(runtime_root: Path, record: SpawnRecord) -> SpawnRecord:
    """Reconcile one active spawn. Is the responsible process alive?"""
    if not is_root_side_effect_process():
        return record
    if not is_active_spawn_status(record.status):
        return record

    now = time.time()
    generic_snapshot = _collect_artifact_snapshot(runtime_root, record, now)
    managed_snapshot = read_managed_primary_snapshot(
        runtime_root,
        record,
        started_epoch=generic_snapshot.started_epoch,
    )
    decision = decide_reconciliation(record, generic_snapshot, managed_snapshot, now)
    if isinstance(decision, Skip):
        return record
    if isinstance(decision, FinalizeSucceededFromReport):
        return _finalize_completed_report(runtime_root, record, generic_snapshot, now)
    if decision.error == "orphan_primary" and (
        managed_snapshot is not None or _is_potential_managed_primary(record)
    ):
        _log_orphan_primary_diagnostics(record, generic_snapshot, managed_snapshot)
    if (
        managed_snapshot is None
        and record.worker_pid is not None
        and record.worker_pid > 0
        and not _is_potential_managed_primary(record)
    ):
        _terminate_worker_pid(record.worker_pid, generic_snapshot.started_epoch)
    return _finalize_failed(runtime_root, record, decision.error, generic_snapshot, now)


def reconcile_spawns(runtime_root: Path, spawns: list[SpawnRecord]) -> list[SpawnRecord]:
    """Batch reconciliation. Only touches active spawns."""
    return [
        (
            reconcile_active_spawn(runtime_root, spawn)
            if is_active_spawn_status(spawn.status)
            else spawn
        )
        for spawn in spawns
    ]
