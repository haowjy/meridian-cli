"""Spawn reconciliation: detect orphaned spawns via process liveness."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

import structlog

from meridian.lib.bootstrap.services import build_spawn_application_service_from_roots
from meridian.lib.core.depth import is_root_side_effect_process
from meridian.lib.core.domain import TerminalSpawnStatus
from meridian.lib.core.spawn_lifecycle import (
    is_active_spawn_status,
    is_terminal_spawn_status,
    resolve_reconciled_terminal_state,
)
from meridian.lib.core.types import SpawnId
from meridian.lib.launch.constants import (
    FINALIZE_EVIDENCE_FILENAME,
    HISTORY_FILENAME,
    LAST_OBSERVED_EVENT_FILENAME,
    OUTPUT_FILENAME,
)
from meridian.lib.platform.locking import lock_file
from meridian.lib.platform.process_scope.base import ProcessScopeSnapshot
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.launch_boundary import LaunchBoundarySummary, read_launch_boundary_summary
from meridian.lib.state.liveness import is_pgid_reachable, is_process_alive
from meridian.lib.state.managed_primary import (
    ManagedPrimaryReconciliationStrategy,
    ManagedPrimarySnapshot,
    ReconciliationContext,
    read_managed_primary_snapshot,
)
from meridian.lib.state.process_scope_projection import mark_scope_released, read_scopes_from_disk
from meridian.lib.state.reaper_cleanup_claim import (
    claim_active_spawn_scopes,
    cleanup_lock_path,
    read_cleanup_claim,
    replace_cleanup_claim,
)
from meridian.lib.state.reconciliation import (
    FinalizeFailed,
    FinalizeFromRunnerExit,
    FinalizeSucceededFromReport,
    ReconciliationDecision,
    Skip,
    completion_or_cancel_decision,
)
from meridian.lib.state.spawn.model import (
    SpawnOrigin,
    SpawnRecord,
    TerminalFacts,
)
from meridian.lib.state.spawn_aggregate import mutate_published_spawn_artifact
from meridian.lib.state.spawn_report import spawn_report_has_durable_completion
from meridian.lib.state.timestamps import iso_timestamp_to_epoch

if TYPE_CHECKING:
    from meridian.lib.state.spawn_store import SpawnScan

logger = structlog.get_logger(__name__)

SPAWN_STARTUP_GRACE_SECS = 15
SPAWN_HEARTBEAT_WINDOW_SECS = 120
SPAWN_POST_RUNNER_EXIT_FINALIZATION_GRACE_SECS = 5
_ACTIVITY_ARTIFACTS: tuple[str, ...] = (
    "heartbeat",
    HISTORY_FILENAME,
    OUTPUT_FILENAME,
    "bash-records.json",
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


class ScopeLiveness(TypedDict):
    """Best-effort liveness projection for a process containment scope."""

    root_alive: bool
    pgid_reachable: bool | None
    likely_serving: bool


def scope_liveness(scope: ProcessScopeSnapshot) -> ScopeLiveness:
    """Project root identity and group reachability into one diagnostic view."""

    root_alive = is_process_alive(
        scope.root_pid,
        created_after_epoch=(scope.root_created_at_epoch or None),
    )
    pgid_reachable = (
        is_pgid_reachable(scope.pgid) if scope.pgid is not None else None
    )
    return {
        "root_alive": root_alive,
        "pgid_reachable": pgid_reachable,
        "likely_serving": root_alive or pgid_reachable is True,
    }


def _runner_exit_at_epoch(runner_exit_at: str | None) -> float | None:
    """Parse runner_exit_at ISO string to epoch seconds."""

    return iso_timestamp_to_epoch(runner_exit_at)


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
    started_epoch = iso_timestamp_to_epoch(record.started_at)
    last_activity_epoch, recent_activity_artifact = _recent_runner_activity(
        runtime_root,
        record.id,
        now,
    )
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
    launch_boundary = read_launch_boundary_summary(runtime_root, SpawnId(record.id))
    return ArtifactSnapshot(
        started_epoch=started_epoch,
        last_activity_epoch=last_activity_epoch,
        recent_activity_artifact=recent_activity_artifact,
        durable_report_completion=spawn_report_has_durable_completion(runtime_root, record.id),
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
    exited_epoch = _runner_exit_at_epoch(
        record.runner_exit.exited_at if record.runner_exit is not None else None
    )
    return (
        exited_epoch is not None
        and now - exited_epoch < SPAWN_POST_RUNNER_EXIT_FINALIZATION_GRACE_SECS
    )


def _finalize_from_runner_exit_decision(record: SpawnRecord) -> FinalizeFromRunnerExit:
    facts = record.runner_exit
    assert facts is not None
    return FinalizeFromRunnerExit(
        status=facts.status,
        exit_code=facts.exit_code,
        error=facts.error,
    )


def decide_generic_reconciliation(
    record: SpawnRecord,
    snapshot: ArtifactSnapshot,
    now: float,
) -> ReconciliationDecision:
    if record.status == "finalizing":
        if snapshot.durable_report_completion:
            decision = completion_or_cancel_decision(record, snapshot.durable_report_completion)
            if decision is not None:
                return decision
        if _has_recent_activity(snapshot):
            return Skip(reason="recent_activity")
        if record.runner_exit is not None:
            return _finalize_from_runner_exit_decision(record)
        if record.cancel_intent is not None:
            decision = completion_or_cancel_decision(record, snapshot.durable_report_completion)
            if decision is not None:
                return decision
        return FinalizeFailed(error="orphan_finalization")

    if record.runner_exit is not None:
        if snapshot.durable_report_completion:
            decision = completion_or_cancel_decision(record, snapshot.durable_report_completion)
            if decision is not None:
                return decision
        if _in_post_runner_exit_finalization_grace(record, now):
            return Skip(reason="post_runner_exit_finalization_grace")
        return _finalize_from_runner_exit_decision(record)

    if _is_pre_worker_launch_boundary_ghost(record, snapshot, now):
        if snapshot.durable_report_completion:
            decision = completion_or_cancel_decision(record, snapshot.durable_report_completion)
            if decision is not None:
                return decision
        return FinalizeFailed(error="launch_boundary_no_takeover")

    runner_pid = record.runner_pid
    if runner_pid is None or runner_pid <= 0:
        if snapshot.durable_report_completion:
            decision = completion_or_cancel_decision(record, snapshot.durable_report_completion)
            if decision is not None:
                return decision
        if _has_recent_activity(snapshot):
            return Skip(reason="recent_activity")
        if _in_startup_grace(snapshot.started_epoch, now):
            return Skip(reason="startup_grace")
        if record.cancel_intent is not None:
            decision = completion_or_cancel_decision(record, snapshot.durable_report_completion)
            if decision is not None:
                return decision
        return FinalizeFailed(error="missing_runner_pid")

    if snapshot.runner_pid_alive:
        if _has_recent_activity(snapshot):
            return Skip(reason="recent_activity")
        return Skip(reason="runner_alive")

    if snapshot.durable_report_completion:
        decision = completion_or_cancel_decision(record, snapshot.durable_report_completion)
        if decision is not None:
            return decision

    if _in_startup_grace(snapshot.started_epoch, now):
        return Skip(reason="startup_grace")
    if record.cancel_intent is not None:
        decision = completion_or_cancel_decision(record, snapshot.durable_report_completion)
        if decision is not None:
            return decision
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
    if record.status == "finalizing" or record.runner_exit is not None:
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
        )

    if (
        _is_potential_managed_primary(record)
        and isinstance(generic_decision, FinalizeFailed)
        and generic_decision.error in {"missing_runner_pid", "orphan_run"}
    ):
        return FinalizeFailed(error="orphan_primary")
    return generic_decision


def _log_orphan_primary_diagnostics(
    runtime_root: Path,
    record: SpawnRecord,
    snapshot: ArtifactSnapshot,
    managed_snapshot: ManagedPrimarySnapshot | None,
) -> None:
    if managed_snapshot is not None:
        metadata = managed_snapshot.metadata
        scopes = read_scopes_from_disk(runtime_root, SpawnId(record.id))
        launcher_pid = metadata.launcher_pid
        launcher_alive = managed_snapshot.launcher_pid_alive if launcher_pid is not None else None
        backend_scope = next(
            (scope for scope in scopes if scope.root_pid == metadata.backend_pid),
            None,
        )
        backend_liveness = (
            scope_liveness(backend_scope) if backend_scope is not None else None
        )
        backend_alive = (
            backend_liveness["likely_serving"]
            if backend_liveness is not None
            else (
                is_process_alive(
                    metadata.backend_pid,
                    created_after_epoch=managed_snapshot.started_epoch,
                )
                if metadata.backend_pid is not None
                else None
            )
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
            backend_root_alive=(
                backend_liveness["root_alive"]
                if backend_liveness is not None
                else backend_alive
            ),
            backend_pgid_reachable=(
                backend_liveness["pgid_reachable"]
                if backend_liveness is not None
                else None
            ),
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


def _read_last_observed_event(runtime_root: Path, spawn_id: str) -> dict[str, object] | None:
    marker_path = (
        runtime_root / "spawns" / spawn_id / LAST_OBSERVED_EVENT_FILENAME
    )
    try:
        parsed: object = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return cast("dict[str, object]", parsed)


def _record_orphan_finalize_evidence(
    runtime_root: Path,
    record: SpawnRecord,
    snapshot: ArtifactSnapshot,
    now: float,
) -> None:
    """Persist the liveness facts observed immediately before orphan cleanup."""

    spawn_id = SpawnId(record.id)
    scopes = [
        scope
        for scope in read_scopes_from_disk(runtime_root, spawn_id)
        if scope.owner_policy == "spawn_owned"
    ]
    child_processes: list[dict[str, object]] = []
    for scope in scopes:
        liveness = scope_liveness(scope)
        child_processes.append(
            {
                "scope_id": scope.scope_id,
                "role": scope.role,
                "pid": scope.root_pid,
                **liveness,
                "alive": liveness["likely_serving"],
            }
        )

    worker_alive: bool | None = None
    if record.worker_pid is not None and record.worker_pid > 0:
        matching_scope = next(
            (item for item in child_processes if item["pid"] == record.worker_pid),
            None,
        )
        worker_alive = (
            bool(matching_scope["likely_serving"])
            if matching_scope is not None
            else is_process_alive(record.worker_pid, created_after_epoch=snapshot.started_epoch)
        )

    heartbeat_epoch = _artifact_mtime_epoch(
        runtime_root / "spawns" / record.id / "heartbeat"
    )
    evidence = {
        "reason": "orphan_run",
        "timestamp": datetime.fromtimestamp(now, UTC).isoformat().replace("+00:00", "Z"),
        "runner": {
            "pid": record.runner_pid,
            "alive": snapshot.runner_pid_alive,
        },
        "worker": {
            "pid": record.worker_pid,
            "alive": worker_alive,
        },
        "child_processes": child_processes,
        "worker_or_backend_alive": worker_alive is True
        or any(bool(item["likely_serving"]) for item in child_processes),
        "heartbeat_age_secs": (
            max(0.0, now - heartbeat_epoch) if heartbeat_epoch is not None else None
        ),
        "last_activity_age_secs": (
            max(0.0, now - snapshot.last_activity_epoch)
            if snapshot.last_activity_epoch is not None
            else None
        ),
        "last_observed_event": _read_last_observed_event(runtime_root, record.id),
    }
    evidence_path = runtime_root / "spawns" / record.id / FINALIZE_EVIDENCE_FILENAME
    try:
        mutate_published_spawn_artifact(
            runtime_root,
            SpawnId(record.id),
            lambda: atomic_write_text(
                evidence_path,
                json.dumps(evidence, separators=(",", ":"), sort_keys=True) + "\n",
            ),
            can_mutate=lambda current: is_active_spawn_status(current.status),
        )
    except Exception:
        logger.warning(
            "Failed to persist orphan finalize evidence.",
            spawn_id=record.id,
            exc_info=True,
        )


def _finalize_and_log(
    project_root: Path,
    runtime_root: Path,
    record: SpawnRecord,
    *,
    status: TerminalSpawnStatus,
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


def _record_with_terminal_state(
    record: SpawnRecord,
    *,
    status: TerminalSpawnStatus,
    exit_code: int,
    error: str | None,
    origin: SpawnOrigin,
    observed_at: str,
) -> SpawnRecord:
    return record.model_copy(
        update={
            "status": status,
            "terminal": TerminalFacts(
                exit_code=exit_code,
                finished_at=observed_at,
                published_at=observed_at,
                error=error,
                origin=origin,
            ),
        }
    )


def peek_reconciled_active_spawn(
    runtime_root: Path,
    record: SpawnRecord,
) -> SpawnRecord:
    """Return a reconciled view without cleanup or state mutation."""

    if not is_active_spawn_status(record.status):
        return record

    now = time.time()
    observed_at = datetime.fromtimestamp(now, UTC).isoformat().replace("+00:00", "Z")
    generic_snapshot = _collect_artifact_snapshot(runtime_root, record, now)
    if (
        record.status == "finalizing"
        and not generic_snapshot.durable_report_completion
        and record.runner_exit is None
        and record.cancel_intent is None
    ):
        return record
    managed_snapshot = read_managed_primary_snapshot(
        runtime_root,
        record,
        started_epoch=generic_snapshot.started_epoch,
    )
    decision = decide_reconciliation(record, generic_snapshot, managed_snapshot, now)
    if isinstance(decision, Skip):
        return record
    if isinstance(decision, FinalizeSucceededFromReport):
        status, exit_code, error = resolve_reconciled_terminal_state(
            durable_report_completion=True,
            fallback_error="harness_completed",
        )
        return _record_with_terminal_state(
            record,
            status=status,
            exit_code=exit_code,
            error=error,
            origin="reconciler",
            observed_at=observed_at,
        )
    if isinstance(decision, FinalizeFromRunnerExit):
        return _record_with_terminal_state(
            record,
            status=decision.status,
            exit_code=decision.exit_code,
            error=decision.error,
            origin="runner",
            observed_at=observed_at,
        )
    return _record_with_terminal_state(
        record,
        status="failed",
        exit_code=decision.exit_code,
        error=decision.error,
        origin="reconciler",
        observed_at=observed_at,
    )


def _fallback_cleanup_scopes(
    runtime_root: Path,
    record: SpawnRecord,
    managed_snapshot: ManagedPrimarySnapshot | None,
) -> tuple[ProcessScopeSnapshot, ...]:
    if read_scopes_from_disk(runtime_root, SpawnId(record.id)):
        return ()
    if managed_snapshot is not None:
        metadata = managed_snapshot.metadata
        scopes: list[ProcessScopeSnapshot] = []
        for scope_id, pid, birth_epoch in (
            ("managed-backend", metadata.backend_pid, metadata.backend_birth_epoch),
            ("managed-tui", metadata.tui_pid, metadata.tui_birth_epoch),
        ):
            if pid is None:
                continue
            scopes.append(
                ProcessScopeSnapshot(
                    scope_id=scope_id,
                    owner_policy="spawn_owned",
                    owner_id=record.id,
                    role="harness_backend",
                    containment="pid_tree_fallback",
                    root_pid=pid,
                    root_created_at_epoch=birth_epoch or 0.0,
                    pgid=None,
                    job_name=None,
                    degraded_reason="managed_primary_metadata_fallback",
                )
            )
        return tuple(scopes)
    if record.worker_pid is None or record.worker_pid <= 0:
        return ()
    return (
        ProcessScopeSnapshot(
            scope_id="legacy-worker",
            owner_policy="spawn_owned",
            owner_id=record.id,
            role="tool_worker",
            containment="pid_tree_fallback",
            root_pid=record.worker_pid,
            root_created_at_epoch=0.0,
            pgid=None,
            job_name=None,
            degraded_reason="legacy_worker_fallback",
        ),
    )


def _claim_reaper_cleanup(
    runtime_root: Path,
    record: SpawnRecord,
    managed_snapshot: ManagedPrimarySnapshot | None = None,
    *,
    include_fallback: bool = False,
) -> None:
    """Persist exact cleanup targets before attempting the terminal CAS."""
    claim_active_spawn_scopes(
        runtime_root,
        SpawnId(record.id),
        extra_scopes=(
            _fallback_cleanup_scopes(runtime_root, record, managed_snapshot)
            if include_fallback
            else ()
        ),
    )


def _cleanup_claimed_scopes(runtime_root: Path, record: SpawnRecord) -> None:
    """Resolve a durable claim once, retaining failed targets for a later pass."""
    spawn_id = SpawnId(record.id)
    with lock_file(cleanup_lock_path(runtime_root, spawn_id), reentrant=False):
        claimed = read_cleanup_claim(runtime_root, spawn_id)
        if not claimed:
            return
        if record.terminal is None or record.terminal.origin != "reconciler":
            replace_cleanup_claim(runtime_root, spawn_id, [])
            return

        from meridian.lib.core.process_cleanup import terminate_scope_sync

        unresolved: list[ProcessScopeSnapshot] = []
        for scope in claimed:
            result = terminate_scope_sync(scope, grace_seconds=5.0, reason="reaper")
            if result.skip_reason == "termination_exception":
                unresolved.append(scope)
                continue
            mark_scope_released(runtime_root, spawn_id, scope.release_id)
        replace_cleanup_claim(runtime_root, spawn_id, unresolved)


def _cleanup_after_finalize(runtime_root: Path, record: SpawnRecord) -> SpawnRecord:
    if is_terminal_spawn_status(record.status):
        _cleanup_claimed_scopes(runtime_root, record)
    return record


def reconcile_active_spawn(
    project_root: Path,
    runtime_root: Path,
    record: SpawnRecord,
) -> SpawnRecord:
    """Reconcile one active spawn. Is the responsible process alive?"""
    if not is_active_spawn_status(record.status):
        if is_root_side_effect_process() and is_terminal_spawn_status(record.status):
            _cleanup_claimed_scopes(runtime_root, record)
        return record

    now = time.time()
    generic_snapshot = _collect_artifact_snapshot(runtime_root, record, now)
    if not is_root_side_effect_process():
        return record
    managed_snapshot = read_managed_primary_snapshot(
        runtime_root,
        record,
        started_epoch=generic_snapshot.started_epoch,
    )
    decision = decide_reconciliation(record, generic_snapshot, managed_snapshot, now)
    if isinstance(decision, Skip):
        return record
    if isinstance(decision, FinalizeFailed) and decision.error == "orphan_run":
        _record_orphan_finalize_evidence(
            runtime_root,
            record,
            generic_snapshot,
            now,
        )
    _claim_reaper_cleanup(
        runtime_root,
        record,
        managed_snapshot if isinstance(decision, FinalizeFailed) else None,
        include_fallback=isinstance(decision, FinalizeFailed),
    )
    if isinstance(decision, FinalizeSucceededFromReport):
        return _cleanup_after_finalize(
            runtime_root,
            _finalize_completed_report(
                project_root,
                runtime_root,
                record,
                generic_snapshot,
                now,
            ),
        )
    if isinstance(decision, FinalizeFromRunnerExit):
        return _cleanup_after_finalize(
            runtime_root,
            _finalize_from_runner_exit(
                project_root,
                runtime_root,
                record,
                decision,
                generic_snapshot,
                now,
            ),
        )
    if decision.error == "orphan_primary" and (
        managed_snapshot is not None or _is_potential_managed_primary(record)
    ):
        _log_orphan_primary_diagnostics(
            runtime_root,
            record,
            generic_snapshot,
            managed_snapshot,
        )
    return _cleanup_after_finalize(
        runtime_root,
        _finalize_failed(
            project_root,
            runtime_root,
            record,
            decision.error,
            generic_snapshot,
            now,
            exit_code=decision.exit_code,
        ),
    )


def reconcile_spawns(
    project_root: Path,
    runtime_root: Path,
    scan: SpawnScan,
) -> SpawnScan:
    """Return a read-only reconciled projection for a batch of spawns.

    List/stat/reference-discovery callers use this helper. It intentionally does
    not finalize spawns or terminate process scopes; cleanup dispatch belongs to
    reconcile_active_spawn().
    """
    _ = project_root
    return replace(
        scan,
        records=tuple(
            peek_reconciled_active_spawn(runtime_root, spawn)
            if is_active_spawn_status(spawn.status)
            else spawn
            for spawn in scan.records
        ),
    )
