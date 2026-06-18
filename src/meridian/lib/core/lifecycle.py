"""Authoritative lifecycle-transition seam for spawn state.

This module is the single service boundary for lifecycle transitions
(``start``, ``mark_running``, ``record_exited``, ``record_runner_exit``,
``mark_finalizing``, ``finalize``, ``cancel``) and post-write lifecycle hook dispatch.
Persistence remains delegated to :mod:`meridian.lib.state.spawn_store`.

Design decisions in effect:
- D1: Service wraps store, not replaces — delegates to spawn_store functions
- D4: Hooks are post-dispatch only — fire after successful store write
- D5: keep update_spawn public — metadata only, no transition
- D9: No async — synchronous methods matching spawn_store
- D12: First-class LifecycleEvent over ad hoc callbacks
- D13: Metrics may be None on spawn.finalized — fire for persisted terminal writes,
  including authoritative replacement of reconciler terminal state

Import note
-----------
Shared spawn record types live in ``meridian.lib.state.spawn.model`` and
launch-policy snapshot lives in ``meridian.lib.core.launch_policy_snapshot``.
Store access stays lazy so package-level compatibility re-exports do not
reintroduce cycles.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

import psutil
import structlog

from meridian.lib.core.spawn_lifecycle import (
    TERMINAL_SPAWN_STATUSES,
    SpawnReservation,
    is_active_spawn_status,
)
from meridian.lib.core.telemetry import (
    CORE_EVENTS,
    SpawnFailure,
    SpawnFailureCategory,
    allocate_spawn_sequence,
    next_spawn_sequence,
    notify_observers,
)
from meridian.lib.core.telemetry import (
    LifecycleEvent as TelemetryLifecycleEvent,
)
from meridian.lib.telemetry import LifecycleCorrelation, bind_lifecycle_correlation

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from meridian.lib.core.clock import Clock
    from meridian.lib.core.domain import SpawnStatus, TokenUsage
    from meridian.lib.hooks.dispatch import HookDispatcher
    from meridian.lib.state.spawn.model import (
        LaunchMode,
        SpawnOrigin,
        SpawnRecord,
    )
    from meridian.lib.state.spawn_store import FinalizeOutcome

logger = structlog.get_logger(__name__)


def _pid_created_at_epoch(pid: int | None) -> float | None:
    if pid is None or pid <= 0:
        return None
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, OSError, ValueError):
        return None


def _spawn_store() -> Any:
    """Return the spawn store module without importing it during lifecycle init."""

    from meridian.lib.state import spawn_store

    return spawn_store


def _spawn_transitions() -> Any:
    """Return pure spawn transition helpers without importing state during init."""

    from meridian.lib.state.spawn import transitions

    return transitions


# ---------------------------------------------------------------------------
# Public type aliases
# ---------------------------------------------------------------------------

EventType = Literal["spawn.created", "spawn.running", "spawn.finalized"]
TerminalStatus = Literal["succeeded", "failed", "cancelled", "timed_out"]
TerminalOrigin = Literal["runner", "launcher", "cancel", "reconciler", "launch_failure"]


type TerminalOutcomeCategory = Literal["succeeded"] | SpawnFailureCategory


_TERMINAL_STATUS_VALUES: frozenset[str] = TERMINAL_SPAWN_STATUSES

# ---------------------------------------------------------------------------
# Event ID generation
# ---------------------------------------------------------------------------


def _event_scope(event_type: str) -> str:
    """Return event namespace scope used in deterministic event IDs."""

    # Keep spawn event IDs byte-for-byte stable with legacy generation.
    if event_type.startswith("spawn."):
        return "spawn"
    return "event"


def generate_lifecycle_event_id(subject_id: str, event_type: str, sequence: int) -> UUID:
    """Generate stable event ID using UUID v5.

    Stability across retries: same subject_id + event_type + sequence
    always produces the same UUID.
    """
    namespace = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # URL namespace
    name = f"meridian:{_event_scope(event_type)}:{subject_id}:{event_type}:{sequence}"
    return uuid.uuid5(namespace, name)


def generate_event_id(spawn_id: str, event_type: str, sequence: int) -> UUID:
    """Backward-compatible wrapper for spawn lifecycle callers."""

    return generate_lifecycle_event_id(spawn_id, event_type, sequence)


# ---------------------------------------------------------------------------
# LifecycleEvent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LifecycleEvent:
    """Immutable event passed to lifecycle hooks.

    Provides stable identity for idempotent execution and full context
    for declarative filters.
    """

    # Identity
    event_id: UUID
    event_type: EventType
    timestamp: datetime

    # Spawn context (always present)
    spawn_id: str
    parent_id: str | None
    chat_id: str | None
    work_id: str | None

    # Execution context (always present)
    agent: str | None
    model: str | None
    harness: str | None

    # Terminal-only fields (None for non-terminal events)
    status: TerminalStatus | None = None
    origin: TerminalOrigin | None = None

    outcome_category: TerminalOutcomeCategory | None = None

    # Metrics (may be None even on terminal events — reducer may merge later)
    duration_secs: float | None = None
    total_cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_is_estimate: bool = False


# ---------------------------------------------------------------------------
# LifecycleHook protocol
# ---------------------------------------------------------------------------


class LifecycleHook(Protocol):
    """Protocol for lifecycle event observers."""

    def on_event(self, event: LifecycleEvent) -> None:
        """Called after successful lifecycle state write.

        Called synchronously after the authoritative store write succeeds.
        Implementations should be fast — defer heavy work to background.

        Exceptions are logged but do not block lifecycle transitions.
        """
        ...


# ---------------------------------------------------------------------------
# SpawnLifecycleService
# ---------------------------------------------------------------------------


class SpawnLifecycleService:
    """Authoritative lifecycle API that wraps spawn_store with hook dispatch.

    This class is the intended caller-facing transition seam. It delegates
    persistence to ``spawn_store`` and only adds consistent post-write
    ``LifecycleEvent`` dispatch semantics.
    """

    def __init__(
        self,
        runtime_root: Path,
        *,
        hooks: list[LifecycleHook] | None = None,
    ) -> None:
        self._runtime_root = runtime_root
        self._hooks = hooks or []
        self._record: SpawnRecord | None = None

    def register_hook(self, hook: LifecycleHook) -> None:
        """Register an additional lifecycle hook after construction."""
        self._hooks.append(hook)

    # ------------------------------------------------------------------
    # Lifecycle transitions
    # ------------------------------------------------------------------

    def start(
        self,
        reservation: SpawnReservation,
        *,
        dispatch_events: bool = True,
    ) -> str:
        """Persist a spawn row from ``reservation`` and optionally dispatch spawn.created."""
        return self._start_from_reservation(reservation, dispatch_events=dispatch_events)

    def reserve(self, reservation: SpawnReservation) -> str:
        """Persist a spawn row without dispatching lifecycle events."""
        return self.start(reservation, dispatch_events=False)

    def announce(self, spawn_id: str) -> None:
        """Dispatch spawn.created and initial telemetry for a reserved row."""
        with bind_lifecycle_correlation(
            self._correlation(operation="announce", spawn_id=spawn_id)
        ):
            record = _spawn_store().get_spawn(self._runtime_root, spawn_id)
            if record is None:
                raise ValueError(f"spawn row not found: {spawn_id}")
            self._record = record
            self._dispatch_spawn_started_events(status=record.status, spawn_id=spawn_id)

    def _start_from_reservation(
        self,
        reservation: SpawnReservation,
        *,
        dispatch_events: bool,
    ) -> str:
        with bind_lifecycle_correlation(
            self._correlation(operation="start", spawn_id=reservation.spawn_id)
        ):
            session_metadata = reservation.session_metadata
            # Authoritative transition write still happens in _spawn_store().
            result_id = _spawn_store().start_spawn(
                self._runtime_root,
                chat_id=reservation.chat_id,
                owner_chat_id=reservation.owner_chat_id,
                parent_id=reservation.parent_id,
                model=session_metadata.model,
                agent=session_metadata.agent,
                agent_path=session_metadata.agent_path or None,
                skills=session_metadata.skills,
                skill_paths=session_metadata.skill_paths,
                harness=session_metadata.harness,
                kind=reservation.kind,
                prompt=reservation.prompt,
                metadata=reservation.metadata,
                desc=reservation.desc,
                work_id=reservation.work_id,
                goal=reservation.goal,
                spawn_id=reservation.spawn_id,
                harness_session_id=reservation.harness_session_id,
                control_root=reservation.control_root,
                task_cwd=reservation.task_cwd,
                execution_cwd=reservation.execution_cwd,
                launch_mode=reservation.launch_mode,
                worker_pid=reservation.worker_pid,
                runner_pid=reservation.runner_pid,
                launch_policy_snapshot=reservation.launch_policy_snapshot,
                status=reservation.status,
                started_at=reservation.started_at,
                clock=reservation.clock,
            )
            allocate_spawn_sequence(str(result_id))
            record = _spawn_store().get_spawn(self._runtime_root, result_id)
            self._record = record
            if dispatch_events:
                self._dispatch_spawn_started_events(
                    status=reservation.status,
                    spawn_id=str(result_id),
                )
            return str(result_id)

    def _dispatch_spawn_started_events(self, *, status: SpawnStatus, spawn_id: str) -> None:
        assert self._record is not None
        event = self._build_event("spawn.created", self._record, spawn_id=spawn_id)
        self._dispatch(event)
        self._emit_telemetry_event("spawn.queued", self._record)
        if status == "running":
            self._emit_telemetry_event("spawn.running", self._record)

    def mark_running(
        self,
        spawn_id: str,
        *,
        launch_mode: LaunchMode | None = None,
        worker_pid: int | None = None,
        runner_pid: int | None = None,
        runner_created_at_epoch: float | None = None,
    ) -> None:
        """Mark a spawn as running and dispatch spawn.running."""
        with bind_lifecycle_correlation(
            self._correlation(operation="mark_running", spawn_id=spawn_id)
        ):
            resolved_runner_created_at_epoch = runner_created_at_epoch
            if runner_pid is not None and resolved_runner_created_at_epoch is None:
                resolved_runner_created_at_epoch = _pid_created_at_epoch(runner_pid)
            if self._owns_record(spawn_id):
                assert self._record is not None
                changed = self._record.status != "running"
                updated = _spawn_transitions().apply_mark_running(
                    self._record,
                    launch_mode=launch_mode,
                    worker_pid=worker_pid,
                    runner_pid=runner_pid,
                    runner_created_at_epoch=resolved_runner_created_at_epoch,
                    validate_status_transition=False,
                )
                if not self._write_owner_record(updated, transition="mark_running"):
                    return
                if changed:
                    event = self._build_event("spawn.running", self._record)
                    self._dispatch(event)
                    self._emit_telemetry_event("spawn.running", self._record)
                return

            # Authoritative transition write still happens in _spawn_store().
            changed, record = _spawn_store().mark_spawn_running_with_snapshot(
                self._runtime_root,
                spawn_id,
                launch_mode=launch_mode,
                worker_pid=worker_pid,
                runner_pid=runner_pid,
                runner_created_at_epoch=resolved_runner_created_at_epoch,
            )
            if changed:
                event = self._build_event("spawn.running", record, spawn_id=spawn_id)
                self._dispatch(event)
                self._emit_telemetry_event("spawn.running", record)

    def record_exited(
        self,
        spawn_id: str,
        *,
        exit_code: int,
        exited_at: str | None = None,
        clock: Clock | None = None,
    ) -> None:
        """Record process exit and emit spawn.process_exited telemetry."""
        with bind_lifecycle_correlation(
            self._correlation(operation="record_exited", spawn_id=spawn_id)
        ):
            if self._owns_record(spawn_id):
                assert self._record is not None
                resolved_exited_at = exited_at or _utc_now_iso(clock)
                updated = _spawn_transitions().apply_record_exited(
                    self._record,
                    spawn_id=spawn_id,
                    exit_code=exit_code,
                    exited_at=resolved_exited_at,
                )
                if not self._write_owner_record(updated, transition="record_exited"):
                    return
                self._emit_telemetry_event(
                    "spawn.process_exited",
                    self._record,
                    payload={"exit_code": exit_code},
                )
                return

            previous = _spawn_store().get_spawn(self._runtime_root, spawn_id)
            # Authoritative transition write still happens in _spawn_store().
            _spawn_store().record_spawn_exited(
                self._runtime_root,
                spawn_id,
                exit_code=exit_code,
                exited_at=exited_at,
                clock=clock,
            )
            record = _record_after_exited_update(
                previous,
                spawn_id=spawn_id,
                exit_code=exit_code,
                exited_at=exited_at,
            )
            self._emit_telemetry_event(
                "spawn.process_exited",
                record,
                payload={"exit_code": exit_code},
            )

    def record_runner_exit(
        self,
        spawn_id: str,
        *,
        status: TerminalStatus,
        exit_code: int,
        error: str | None = None,
        exited_at: str | None = None,
        clock: Clock | None = None,
    ) -> bool:
        """Persist runner-resolved terminal intent before finalization."""

        with bind_lifecycle_correlation(
            self._correlation(operation="record_runner_exit", spawn_id=spawn_id)
        ):
            resolved_exited_at = exited_at or _utc_now_iso(clock)
            if self._owns_record(spawn_id):
                assert self._record is not None
                if not is_active_spawn_status(self._record.status):
                    return False
                updated = _spawn_transitions().apply_runner_exit(
                    self._record,
                    spawn_id=spawn_id,
                    status=status,
                    exit_code=exit_code,
                    error=error,
                    exited_at=resolved_exited_at,
                )
                if not self._write_owner_record(updated, transition="record_runner_exit"):
                    return False
                self._emit_telemetry_event(
                    "spawn.updated",
                    self._record,
                    payload={
                        "runner_exit_status": status,
                        "runner_exit_code": exit_code,
                        "runner_exit_error": error,
                        "runner_exit_at": resolved_exited_at,
                    },
                )
                return True

            record = _spawn_store().record_runner_exit(
                self._runtime_root,
                spawn_id,
                status=status,
                exit_code=exit_code,
                error=error,
                exited_at=resolved_exited_at,
                clock=clock,
            )
            if record is None:
                return False
            self._emit_telemetry_event(
                "spawn.updated",
                record,
                payload={
                    "runner_exit_status": status,
                    "runner_exit_code": exit_code,
                    "runner_exit_error": error,
                    "runner_exit_at": resolved_exited_at,
                },
            )
            return True

    def request_cancel(
        self,
        spawn_id: str,
        *,
        exit_code: int = 130,
        error: str | None = "cancelled",
        requested_by: Literal["user", "system"] = "user",
        requested_at: str | None = None,
        clock: Clock | None = None,
    ) -> SpawnRecord | None:
        """Persist pre-terminal spawn-level cancel intent."""

        with bind_lifecycle_correlation(
            self._correlation(operation="request_cancel", spawn_id=spawn_id)
        ):
            record = _spawn_store().record_cancel_intent(
                self._runtime_root,
                spawn_id,
                exit_code=exit_code,
                error=error,
                requested_by=requested_by,
                requested_at=requested_at,
                clock=clock,
            )
            if record is None:
                return None
            self._emit_telemetry_event(
                "spawn.updated",
                record,
                payload={
                    "cancel_intent": {
                        "requested_at": record.cancel_intent.requested_at,
                        "exit_code": record.cancel_intent.exit_code,
                        "error": record.cancel_intent.error,
                        "requested_by": record.cancel_intent.requested_by,
                    }
                    if record.cancel_intent is not None
                    else None,
                },
            )
            return record

    def finalize(
        self,
        spawn_id: str,
        status: SpawnStatus,
        exit_code: int,
        *,
        origin: SpawnOrigin,
        duration_secs: float | None = None,
        usage: TokenUsage | None = None,
        finished_at: str | None = None,
        error: str | None = None,
        clock: Clock | None = None,
    ) -> FinalizeOutcome:
        """Finalize a spawn and dispatch spawn.finalized for persisted terminal writes."""
        requested_category = self._terminal_outcome_category(
            status=status,
            origin=origin,
            error=error,
        )
        with bind_lifecycle_correlation(
            self._correlation(
                operation="finalize",
                spawn_id=spawn_id,
                outcome_category=requested_category,
                terminal_status=status,
                terminal_origin=origin,
            )
        ):
            outcome = _spawn_store().finalize_spawn(
                self._runtime_root,
                spawn_id,
                status,
                exit_code,
                origin=origin,
                duration_secs=duration_secs,
                usage=usage,
                finished_at=finished_at,
                error=error,
                clock=clock,
            )
            if outcome.wrote and outcome.snapshot is not None:
                if self._owns_record(spawn_id):
                    self._record = outcome.snapshot
                self._reconcile_failure_sentinel(
                    outcome.snapshot,
                    origin=origin,
                    error=error,
                )
                event = self._build_event("spawn.finalized", outcome.snapshot)
                self._dispatch(event)
                self._emit_telemetry_event_for_record(
                    f"spawn.{outcome.snapshot.status}", outcome.snapshot
                )
            return outcome

    def mark_finalizing(self, spawn_id: str) -> bool:
        """CAS transition running -> finalizing.  No lifecycle event dispatched."""
        with bind_lifecycle_correlation(
            self._correlation(operation="mark_finalizing", spawn_id=spawn_id)
        ):
            if self._owns_record(spawn_id):
                assert self._record is not None
                if self._record.status != "running":
                    return False
                updated = _spawn_transitions().apply_mark_finalizing(self._record)
                if not self._write_owner_record(updated, transition="mark_finalizing"):
                    return False
                self._emit_telemetry_event("spawn.finalizing", self._record)
                return True

            # Authoritative transition write still happens in _spawn_store().
            transitioned, record = _spawn_store().mark_finalizing_with_snapshot(
                self._runtime_root,
                spawn_id,
            )
            if transitioned:
                self._emit_telemetry_event("spawn.finalizing", record)
            return transitioned

    def cancel(
        self,
        spawn_id: str,
        exit_code: int = 130,
        *,
        duration_secs: float | None = None,
        usage: TokenUsage | None = None,
        finished_at: str | None = None,
        error: str | None = None,
        clock: Clock | None = None,
    ) -> bool:
        """Cancel a spawn — convenience for finalize(status='cancelled', origin='cancel')."""
        with bind_lifecycle_correlation(
            self._correlation(
                operation="cancel",
                spawn_id=spawn_id,
                outcome_category=SpawnFailureCategory.CANCELLATION,
                terminal_status="cancelled",
                terminal_origin="cancel",
            )
        ):
            outcome = self.finalize(
                spawn_id,
                "cancelled",
                exit_code,
                origin="cancel",
                duration_secs=duration_secs,
                usage=usage,
                finished_at=finished_at,
                error=error,
                clock=clock,
            )
            return outcome.transitioned

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _correlation(
        self,
        *,
        operation: str,
        record: SpawnRecord | None = None,
        spawn_id: str | None = None,
        event_type: str | None = None,
        outcome_category: TerminalOutcomeCategory | None = None,
        terminal_status: str | None = None,
        terminal_origin: str | None = None,
    ) -> LifecycleCorrelation:
        resolved_spawn_id = spawn_id or (record.id if record is not None else None)
        resolved_status = terminal_status
        if (
            resolved_status is None
            and record is not None
            and record.status in _TERMINAL_STATUS_VALUES
        ):
            resolved_status = record.status
        resolved_origin = terminal_origin
        if resolved_origin is None and record is not None:
            resolved_origin = record.terminal_origin
        return LifecycleCorrelation(
            spawn_id=resolved_spawn_id,
            parent_id=record.parent_id if record is not None else None,
            chat_id=record.chat_id if record is not None else None,
            work_id=record.work_id if record is not None else None,
            agent=record.agent if record is not None else None,
            model=record.model if record is not None else None,
            harness=record.harness if record is not None else None,
            lifecycle_operation=operation,
            lifecycle_event=event_type,
            terminal_status=resolved_status,
            terminal_origin=resolved_origin,
            failure_category=(str(outcome_category) if outcome_category is not None else None),
        )

    def _terminal_outcome_category(
        self,
        *,
        status: str,
        origin: str | None,
        error: str | None = None,
    ) -> TerminalOutcomeCategory:
        return _terminal_outcome_category(status=status, origin=origin, error=error)

    def _build_terminal_failure_diagnostic(
        self,
        record: SpawnRecord,
        *,
        origin: TerminalOrigin | None = None,
        error: str | None = None,
    ) -> SpawnFailure:
        outcome_category = _terminal_failure_category(
            status=record.status,
            origin=origin or record.terminal_origin,
        )
        correlation = self._correlation(
            operation="finalize",
            record=record,
            event_type="spawn.finalized",
            outcome_category=outcome_category,
        )
        return SpawnFailure(
            spawn_id=record.id,
            ts=datetime.now(tz=UTC),
            exit_code=record.exit_code,
            reason=record.error or error or record.terminal_origin or origin or "spawn failed",
            category=outcome_category or SpawnFailureCategory.UNKNOWN_FAILURE,
            status=record.status,
            origin=record.terminal_origin or origin,
            correlation=correlation.to_context(),
            metadata={"origin": record.terminal_origin or origin},
        )

    def bootstrap_from_disk(self, spawn_id: str) -> SpawnRecord | None:
        """Load owner state once for workers that did not call start()."""

        self._record = _spawn_store().get_spawn(
            self._runtime_root,
            spawn_id,
        )
        return self._record

    def _owns_record(self, spawn_id: str) -> bool:
        return self._record is not None and self._record.id == str(spawn_id)

    def _write_owner_record(self, record: SpawnRecord, *, transition: str) -> bool:
        try:
            from meridian.lib.state.paths import RuntimePaths
            from meridian.lib.state.spawn.repository import write_state

            write_state(RuntimePaths.from_root_dir(self._runtime_root).spawns_dir, record)
        except ValueError:
            logger.debug(
                "Owner lifecycle write dropped by terminal monotonicity guard.",
                spawn_id=record.id,
                transition=transition,
            )
            return False
        self._record = record
        return True

    def _read_owner_record_from_disk(self, spawn_id: str) -> SpawnRecord | None:
        from meridian.lib.state.paths import RuntimePaths
        from meridian.lib.state.spawn.repository import read_state

        return read_state(RuntimePaths.from_root_dir(self._runtime_root).spawns_dir, spawn_id)

    def _reconcile_failure_sentinel(
        self,
        record: SpawnRecord,
        *,
        origin: TerminalOrigin | None = None,
        error: str | None = None,
    ) -> None:
        if record.status == "failed":
            _write_failure_sentinel(
                self._runtime_root,
                record.id,
                self._build_terminal_failure_diagnostic(
                    record,
                    origin=origin,
                    error=error,
                ),
            )
            return
        _delete_failure_sentinel(self._runtime_root, record.id)

    def _dispatch(self, event: LifecycleEvent) -> None:
        correlation = LifecycleCorrelation(
            spawn_id=event.spawn_id,
            parent_id=event.parent_id,
            chat_id=event.chat_id,
            work_id=event.work_id,
            agent=event.agent,
            model=event.model,
            harness=event.harness,
            lifecycle_operation="dispatch",
            lifecycle_event=event.event_type,
            terminal_status=event.status,
            terminal_origin=event.origin,
            failure_category=(
                str(event.outcome_category) if event.outcome_category is not None else None
            ),
        )
        with bind_lifecycle_correlation(correlation):
            for hook in self._hooks:
                try:
                    hook.on_event(event)
                except Exception:
                    logger.exception(
                        "Lifecycle hook raised exception; transition continues",
                        event_id=str(event.event_id),
                        event_type=event.event_type,
                        spawn_id=event.spawn_id,
                    )

    def _emit_telemetry_event(
        self,
        event_name: str,
        record: SpawnRecord | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if event_name not in CORE_EVENTS and event_name != "spawn.updated":
            return
        if record is None:
            return
        self._emit_telemetry_event_for_record(event_name, record, payload=payload)

    def _emit_telemetry_event_for_record(
        self,
        event_name: str,
        record: SpawnRecord,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if event_name not in CORE_EVENTS and event_name != "spawn.updated":
            return
        if payload is None and event_name in {
            "spawn.succeeded",
            "spawn.failed",
            "spawn.cancelled",
            "spawn.timed_out",
        }:
            payload = _terminal_telemetry_payload(record)
        _emit_lifecycle_event(event_name, record, payload=payload)

    def _build_event(
        self,
        event_type: EventType,
        record: SpawnRecord | None,
        *,
        spawn_id: str | None = None,
    ) -> LifecycleEvent:
        resolved_spawn_id = spawn_id or (record.id if record is not None else None)
        if resolved_spawn_id is None:
            raise ValueError("spawn_id is required when building an event without a record")

        # Terminal-only fields
        status: TerminalStatus | None = None
        origin: TerminalOrigin | None = None
        duration_secs: float | None = None
        total_cost_usd: float | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        cache_read_input_tokens: int | None = None
        cache_creation_input_tokens: int | None = None
        reasoning_tokens: int | None = None
        cost_is_estimate = False
        outcome_category: TerminalOutcomeCategory | None = None

        if event_type == "spawn.finalized" and record is not None:
            rec_status = record.status
            if rec_status in _TERMINAL_STATUS_VALUES:
                status = rec_status  # type: ignore[assignment]
            origin = record.terminal_origin  # type: ignore[assignment]
            duration_secs = record.duration_secs
            total_cost_usd = record.total_cost_usd
            input_tokens = record.input_tokens
            output_tokens = record.output_tokens
            cache_read_input_tokens = record.cache_read_input_tokens
            cache_creation_input_tokens = record.cache_creation_input_tokens
            reasoning_tokens = record.reasoning_tokens
            cost_is_estimate = record.cost_is_estimate
            outcome_category = self._terminal_outcome_category(
                status=record.status,
                origin=record.terminal_origin,
                error=record.error,
            )

        return LifecycleEvent(
            event_id=generate_event_id(resolved_spawn_id, event_type, 0),
            event_type=event_type,
            timestamp=datetime.now(tz=UTC),
            spawn_id=resolved_spawn_id,
            parent_id=record.parent_id if record is not None else None,
            chat_id=record.chat_id if record is not None else None,
            work_id=record.work_id if record is not None else None,
            agent=record.agent if record is not None else None,
            model=record.model if record is not None else None,
            harness=record.harness if record is not None else None,
            status=status,
            origin=origin,
            outcome_category=outcome_category,
            duration_secs=duration_secs,
            total_cost_usd=total_cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            reasoning_tokens=reasoning_tokens,
            cost_is_estimate=cost_is_estimate,
        )


def _record_after_exited_update(
    record: SpawnRecord | None,
    *,
    spawn_id: str,
    exit_code: int,
    exited_at: str | None,
) -> SpawnRecord | None:
    if record is None:
        return None
    updates: dict[str, object] = {
        "id": spawn_id,
        "last_attempt_exit_code": exit_code,
    }
    if exited_at is not None:
        updates["last_attempt_exited_at"] = exited_at
    return record.model_copy(update=updates)


def _utc_now_iso(clock: Clock | None = None) -> str:
    if clock is not None:
        return clock.utc_now_iso()
    return datetime.now(tz=UTC).isoformat()


def _emit_lifecycle_event(
    event_name: str,
    spawn: SpawnRecord,
    *,
    payload: dict[str, Any] | None = None,
) -> None:
    """Emit a telemetry lifecycle event through the observer registry."""
    event = TelemetryLifecycleEvent(
        event=event_name,
        spawn_id=str(spawn.id),
        harness_id=spawn.harness or "",
        model=spawn.model or "",
        agent=spawn.agent,
        ts=datetime.now(tz=UTC),
        seq=next_spawn_sequence(spawn.id),
        payload=payload or {},
    )
    notify_observers(event)


def _terminal_failure_category(
    *,
    status: str,
    origin: str | None,
    error: str | None = None,
) -> SpawnFailureCategory | None:
    _ = error
    if status == "succeeded":
        return None
    if status == "cancelled" or origin == "cancel":
        return SpawnFailureCategory.CANCELLATION
    if origin == "launch_failure":
        return SpawnFailureCategory.LAUNCH_FAILURE
    if origin == "reconciler":
        return SpawnFailureCategory.RECONCILER_ORPHAN
    if status != "failed":
        return SpawnFailureCategory.UNKNOWN_FAILURE
    if origin == "runner":
        return SpawnFailureCategory.HARNESS_FAILURE
    return SpawnFailureCategory.UNKNOWN_FAILURE


def _terminal_outcome_category(
    *,
    status: str,
    origin: str | None,
    error: str | None,
) -> TerminalOutcomeCategory:
    failure_category = _terminal_failure_category(status=status, origin=origin, error=error)
    if failure_category is not None:
        return failure_category
    return "succeeded"


def _terminal_telemetry_payload(spawn: SpawnRecord) -> dict[str, Any]:
    """Build sparse terminal lifecycle payload for observer projections."""
    payload: dict[str, Any] = {
        "status": spawn.status,
    }
    failure_category = _terminal_failure_category(
        status=spawn.status,
        origin=spawn.terminal_origin,
        error=spawn.error,
    )
    if failure_category is not None:
        payload["category"] = str(failure_category)
    if spawn.exit_code is not None:
        payload["exit_code"] = spawn.exit_code
    if spawn.duration_secs is not None:
        payload["duration_secs"] = spawn.duration_secs
    if spawn.total_cost_usd is not None:
        payload["total_cost_usd"] = spawn.total_cost_usd
    if spawn.input_tokens is not None:
        payload["input_tokens"] = spawn.input_tokens
    if spawn.output_tokens is not None:
        payload["output_tokens"] = spawn.output_tokens
    if spawn.cache_read_input_tokens is not None:
        payload["cache_read_input_tokens"] = spawn.cache_read_input_tokens
    if spawn.cache_creation_input_tokens is not None:
        payload["cache_creation_input_tokens"] = spawn.cache_creation_input_tokens
    if spawn.reasoning_tokens is not None:
        payload["reasoning_tokens"] = spawn.reasoning_tokens
    if spawn.cost_is_estimate:
        payload["cost_is_estimate"] = True
    if spawn.error:
        payload["reason"] = spawn.error
    return payload


def _write_failure_sentinel(
    runtime_root: Path,
    spawn_id: str,
    failure: SpawnFailure,
) -> None:
    """Best-effort write of failure sentinel.

    Does not propagate exceptions; terminal state writes take priority.
    """
    try:
        from meridian.lib.state.paths import RuntimePaths

        sentinel_path = (
            RuntimePaths.from_root_dir(runtime_root).spawns_dir / spawn_id / "failure.json"
        )
        sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(failure)
        data["ts"] = failure.ts.isoformat()
        sentinel_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Failed to write failure sentinel for %s", spawn_id)


def _delete_failure_sentinel(runtime_root: Path, spawn_id: str) -> None:
    """Best-effort removal of stale failure sentinel after non-failed replacement."""
    try:
        from meridian.lib.state.paths import RuntimePaths

        sentinel_path = (
            RuntimePaths.from_root_dir(runtime_root).spawns_dir / spawn_id / "failure.json"
        )
        sentinel_path.unlink(missing_ok=True)
    except Exception:
        logger.exception("Failed to remove failure sentinel for %s", spawn_id)


# ---------------------------------------------------------------------------
# Lifecycle service factory seam
# ---------------------------------------------------------------------------


def _hooks_dispatch_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether hook dispatch should run globally."""

    scope = os.environ if env is None else env
    value = scope.get("MERIDIAN_HOOKS_ENABLED")
    if value is None:
        return True
    return value.strip().lower() != "false"


def get_hook_dispatcher(project_root: Path, runtime_root: Path) -> HookDispatcher | None:
    """Build a hook dispatcher when hook dispatch is enabled."""

    if not _hooks_dispatch_enabled():
        return None

    try:
        # Imported lazily to avoid lifecycle <-> hooks circular import at module import time.
        from meridian.lib.hooks.dispatch import HookDispatcher
    except Exception:
        logger.exception(
            "Failed to import hook dispatcher; continuing without lifecycle hook dispatch."
        )
        return None

    return HookDispatcher(project_root, runtime_root)


def create_lifecycle_service(
    project_root: Path,
    runtime_root: Path,
) -> SpawnLifecycleService:
    """Create a spawn lifecycle service with centralized hook wiring."""

    dispatcher = get_hook_dispatcher(project_root, runtime_root)
    hooks: list[LifecycleHook] | None = [dispatcher] if dispatcher is not None else None
    return SpawnLifecycleService(
        runtime_root,
        hooks=hooks,
    )
