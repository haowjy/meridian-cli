"""Spawn reconciliation: detect orphaned spawns via process liveness."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog

from meridian.lib.bootstrap.services import build_spawn_application_service_from_roots
from meridian.lib.core.depth import is_root_side_effect_process
from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.spawn_lifecycle import (
    has_durable_report_completion,
    is_active_spawn_status,
    resolve_reconciled_terminal_state,
)
from meridian.lib.core.types import SpawnId
from meridian.lib.launch.constants import (
    HISTORY_FILENAME,
    OUTPUT_FILENAME,
    PI_LIFECYCLE_EVENTS_FILENAME,
)
from meridian.lib.state.launch_boundary import LaunchBoundarySummary, read_launch_boundary_summary
from meridian.lib.state.liveness import is_process_alive
from meridian.lib.state.managed_primary import (
    ManagedPrimaryReconciliationStrategy,
    ManagedPrimarySnapshot,
    ReconciliationContext,
    read_managed_primary_snapshot,
    terminate_managed_primary_processes,
)
from meridian.lib.state.spawn.model import SpawnRecord

logger = structlog.get_logger(__name__)

SPAWN_STARTUP_GRACE_SECS = 15
SPAWN_HEARTBEAT_WINDOW_SECS = 120
SPAWN_POST_RUNNER_EXIT_FINALIZATION_GRACE_SECS = 5
_ACTIVITY_ARTIFACTS: tuple[str, ...] = (
    "heartbeat",
    HISTORY_FILENAME,
    OUTPUT_FILENAME,
    PI_LIFECYCLE_EVENTS_FILENAME,
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
    launch_boundary: LaunchBoundarySummary


@dataclass(frozen=True)
class Skip:
    reason: str


@dataclass(frozen=True)
class FinalizeFailed:
    error: str
    exit_code: int = 1


@dataclass(frozen=True)
class FinalizeSucceededFromReport:
    pass


@dataclass(frozen=True)
class FinalizeFromRunnerExit:
    status: SpawnStatus
    exit_code: int
    error: str | None


type ReconciliationDecision = (
    Skip | FinalizeFailed | FinalizeSucceededFromReport | FinalizeFromRunnerExit
)


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


def _runner_exit_at_epoch(runner_exit_at: str | None) -> float | None:
    """Parse runner_exit_at ISO string to epoch seconds."""

    return _started_at_epoch(runner_exit_at)


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
        if now - mtime_epoch <= SPAWN_HEARTBEAT_WINDOW_SECS:
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
    runner_created_at_epoch = (
        record.runner_created_at_epoch
        if record.runner_created_at_epoch is not None
        else started_epoch
    )
    if record.status != "finalizing" and record.runner_pid is not None and record.runner_pid > 0:
        runner_pid_alive = is_process_alive(
            record.runner_pid,
            created_after_epoch=runner_created_at_epoch,
        )
    launch_boundary = read_launch_boundary_summary(runtime_root, record.id)
    return ArtifactSnapshot(
        started_epoch=started_epoch,
        last_activity_epoch=last_activity_epoch,
        recent_activity_artifact=recent_activity_artifact,
        durable_report_completion=has_durable_report_completion(report_text),
        runner_pid_alive=runner_pid_alive,
        launch_boundary=launch_boundary,
    )


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


def _is_pre_worker_launch_boundary_ghost(
    record: SpawnRecord,
    snapshot: ArtifactSnapshot,
    now: float,
) -> bool:
    if record.launch_mode != "background":
        return False
    if _has_recent_activity(snapshot):
        return False
    if _in_startup_grace(snapshot.started_epoch, now):
        return False
    boundary = snapshot.launch_boundary
    if not boundary.has_events or boundary.has_worker_takeover:
        return False
    runner_pid = (
        record.runner_pid if record.runner_pid is not None and record.runner_pid > 0 else None
    )
    if runner_pid is None:
        return True
    parent_observed_launcher_pid = boundary.parent_observed_launcher_pid
    if parent_observed_launcher_pid is None:
        return False
    return runner_pid == parent_observed_launcher_pid


def _in_post_runner_exit_finalization_grace(record: SpawnRecord, now: float) -> bool:
    exited_epoch = _runner_exit_at_epoch(record.runner_exit_at)
    return (
        exited_epoch is not None
        and now - exited_epoch < SPAWN_POST_RUNNER_EXIT_FINALIZATION_GRACE_SECS
    )


def _finalize_from_runner_exit_decision(record: SpawnRecord) -> FinalizeFromRunnerExit:
    status = record.runner_exit_status
    if status not in {"succeeded", "failed", "cancelled"}:
        return FinalizeFromRunnerExit(status="failed", exit_code=1, error="orphan_run")
    if record.runner_exit_code is not None:
        exit_code = record.runner_exit_code
    elif status == "succeeded":
        exit_code = 0
    elif status == "cancelled":
        exit_code = 130
    else:
        exit_code = 1
    return FinalizeFromRunnerExit(
        status=status,
        exit_code=exit_code,
        error=record.runner_exit_error,
    )


def decide_generic_reconciliation(
    record: SpawnRecord,
    snapshot: ArtifactSnapshot,
    now: float,
) -> ReconciliationDecision:
    if record.status == "finalizing":
        if _has_recent_activity(snapshot):
            return Skip(reason="recent_activity")
        if record.runner_exit_status is not None:
            return _finalize_from_runner_exit_decision(record)
        return FinalizeFailed(error="orphan_finalization")

    if record.runner_exit_status is not None:
        if _in_post_runner_exit_finalization_grace(record, now):
            return Skip(reason="post_runner_exit_finalization_grace")
        return _finalize_from_runner_exit_decision(record)

    if _is_pre_worker_launch_boundary_ghost(record, snapshot, now):
        if snapshot.durable_report_completion:
            return FinalizeSucceededFromReport()
        return FinalizeFailed(error="launch_boundary_no_takeover")

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
    return FinalizeFailed(
        error="orphan_run",
        exit_code=record.last_attempt_exit_code or 1,
    )


def decide_reconciliation(
    record: SpawnRecord,
    generic_snapshot: ArtifactSnapshot,
    managed_snapshot: ManagedPrimarySnapshot | None,
    now: float,
) -> ReconciliationDecision:
    """Unified reconciliation dispatcher."""

    generic_decision = decide_generic_reconciliation(record, generic_snapshot, now)
    if record.status == "finalizing" or record.runner_exit_status is not None:
        return generic_decision

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
        launcher_alive = managed_snapshot.launcher_pid_alive if launcher_pid is not None else None
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
            "Managed primary launcher dead during orphan reconciliation.",
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
        record.runner_pid if record.runner_pid is not None and record.runner_pid > 0 else None
    )
    launcher_alive = snapshot.runner_pid_alive if launcher_pid is not None else None
    logger.warning(
        ("Managed primary candidate reconciled without readable metadata."),
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
    project_root: Path,
    runtime_root: Path,
    record: SpawnRecord,
    *,
    status: SpawnStatus,
    exit_code: int,
    error: str | None,
    reason: str,
    snapshot: ArtifactSnapshot,
    now: float,
) -> SpawnRecord:
    service = build_spawn_application_service_from_roots(project_root, runtime_root)
    outcome = asyncio.run(
        service.complete_spawn(
            SpawnId(record.id),
            status,
            exit_code,
            origin="reconciler",
            error=error,
        )
    )
    resolved_record = outcome.snapshot or record
    if not outcome.wrote:
        return resolved_record
    if outcome.snapshot is None:
        return record
    inactivity_secs = (
        max(0.0, now - snapshot.last_activity_epoch)
        if snapshot.last_activity_epoch is not None
        else None
    )
    logger.debug(
        "Reconciled active spawn.",
        spawn_id=record.id,
        status=status,
        reason=reason,
        heartbeat_window_secs=SPAWN_HEARTBEAT_WINDOW_SECS,
        last_activity_epoch=snapshot.last_activity_epoch,
        recent_activity_artifact=snapshot.recent_activity_artifact,
        inactivity_secs=inactivity_secs,
    )
    return resolved_record


def _finalize_failed(
    project_root: Path,
    runtime_root: Path,
    record: SpawnRecord,
    error: str,
    snapshot: ArtifactSnapshot,
    now: float,
    exit_code: int = 1,
) -> SpawnRecord:
    return _finalize_and_log(
        project_root,
        runtime_root,
        record,
        status="failed",
        exit_code=exit_code,
        error=error,
        reason=error,
        snapshot=snapshot,
        now=now,
    )


def _finalize_completed_report(
    project_root: Path,
    runtime_root: Path,
    record: SpawnRecord,
    snapshot: ArtifactSnapshot,
    now: float,
) -> SpawnRecord:
    status, exit_code, error = resolve_reconciled_terminal_state(
        durable_report_completion=True,
        fallback_error="harness_completed",
    )
    return _finalize_and_log(
        project_root,
        runtime_root,
        record,
        status=status,
        exit_code=exit_code,
        error=error,
        reason="report_completed",
        snapshot=snapshot,
        now=now,
    )


def _finalize_from_runner_exit(
    project_root: Path,
    runtime_root: Path,
    record: SpawnRecord,
    decision: FinalizeFromRunnerExit,
    snapshot: ArtifactSnapshot,
    now: float,
) -> SpawnRecord:
    return _finalize_and_log(
        project_root,
        runtime_root,
        record,
        status=decision.status,
        exit_code=decision.exit_code,
        error=decision.error,
        reason="runner_exit",
        snapshot=snapshot,
        now=now,
    )


def _in_startup_grace(started_epoch: float | None, now: float) -> bool:
    return started_epoch is not None and now - started_epoch < SPAWN_STARTUP_GRACE_SECS


def reconcile_active_spawn(
    project_root: Path,
    runtime_root: Path,
    record: SpawnRecord,
) -> SpawnRecord:
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
        return _finalize_completed_report(
            project_root,
            runtime_root,
            record,
            generic_snapshot,
            now,
        )
    if isinstance(decision, FinalizeFromRunnerExit):
        return _finalize_from_runner_exit(
            project_root,
            runtime_root,
            record,
            decision,
            generic_snapshot,
            now,
        )
    if decision.error == "orphan_primary" and (
        managed_snapshot is not None or _is_potential_managed_primary(record)
    ):
        _log_orphan_primary_diagnostics(record, generic_snapshot, managed_snapshot)
    from meridian.lib.core.process_cleanup import terminate_spawn_scopes

    terminate_spawn_scopes(
        runtime_root,
        record,
        reason="reaper",
        grace_seconds=5.0,
    )
    if managed_snapshot is not None:
        signaled = terminate_managed_primary_processes(
            managed_snapshot.metadata,
            started_epoch=managed_snapshot.started_epoch,
            include_launcher=True,
            include_runtime_children=True,
        )
        if signaled:
            logger.debug(
                "Terminated managed primary tracked processes during orphan reconciliation.",
                spawn_id=record.id,
                signaled_pids=signaled,
            )
    elif _is_potential_managed_primary(record):
        from meridian.lib.state.primary_meta import read_primary_metadata

        metadata = read_primary_metadata(runtime_root, record.id)
        if metadata is not None:
            signaled = terminate_managed_primary_processes(
                metadata,
                started_epoch=generic_snapshot.started_epoch,
                include_launcher=True,
                include_runtime_children=True,
            )
            if signaled:
                logger.debug(
                    "Terminated managed primary processes during orphan reconciliation.",
                    spawn_id=record.id,
                    signaled_pids=signaled,
                    metadata_source="late_read",
                )
        else:
            logger.warning(
                "Managed primary orphaned; metadata unreadable, zombie processes may remain.",
                spawn_id=record.id,
            )
    return _finalize_failed(
        project_root,
        runtime_root,
        record,
        decision.error,
        generic_snapshot,
        now,
        exit_code=decision.exit_code,
    )


def reconcile_spawns(
    project_root: Path,
    runtime_root: Path,
    spawns: list[SpawnRecord],
) -> list[SpawnRecord]:
    """Batch reconciliation. Only touches active spawns."""
    return [
        (
            reconcile_active_spawn(project_root, runtime_root, spawn)
            if is_active_spawn_status(spawn.status)
            else spawn
        )
        for spawn in spawns
    ]
