"""Pi completion policy, deadlines, lifecycle phases, and exit decisions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.core.types import SpawnId
from meridian.lib.state.spawn_signals import consume_resident_signals
from meridian.lib.streaming.completion_contracts import (
    AssessmentTrigger,
    CompletionDirectives,
    CompletionEvaluation,
    CompletionState,
    EvidenceActivity,
    NudgeUrgency,
    ProfileDecision,
    ProfileExitDecision,
    WorkAssessment,
)
from meridian.lib.streaming.completion_nudge import (
    COMPLETION_NUDGE_INTERVAL_SECONDS,
    PI_COMPLETION_NUDGE_MESSAGE,
)
from meridian.lib.streaming.drain_policy import (
    DrainPolicy,
    PiRpcQuiescenceDrainPolicy,
)
from meridian.lib.streaming.pi_quiescence import PiQuiescenceTracker
from meridian.lib.streaming.pi_subspawn_tracker import PiSubspawnTracker
from meridian.lib.streaming.pi_work_ledger import (
    PiPendingNotification,
    PiPrivateWorkLedger,
)

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessEvent
    from meridian.lib.harness.semantics import TerminalEventOutcome
    from meridian.lib.streaming.spawn_session import DrainOutcome

PI_MICRO_DRAIN_TIMEOUT_SECONDS: float = 0.05
PI_EVIDENCE_UNREADABLE_TIMEOUT_SECONDS: float = 300.0
PI_DONE_NUDGE_IDLE_DELAY_SECONDS: float = 5.0

EmitPiPhase = Callable[..., None]
SendPiDoneNudge = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class PiOutstandingWork:
    """Classify Pi work that remains after the parent goes idle."""

    spawn_children: bool
    non_spawn_processes: bool
    unknown_spawn_children: bool = False


@dataclass(frozen=True)
class ChildTimeoutTelemetry:
    active_tracked_count: int
    elapsed_seconds: float
    timeout_seconds: float


class PiCompletionEvidenceView(Protocol):
    tracker: PiSubspawnTracker
    quiescence_tracker: PiQuiescenceTracker
    session_seen: bool
    session_phase_emitted: bool

    def has_pending_children(self) -> bool: ...

    def pending_child_count(self) -> int: ...


class PiCompletionCleanupPort(Protocol):
    terminate_children: Callable[[PiPrivateWorkLedger, str], Awaitable[None]] | None

    def prepare_child_timeout(self, telemetry: ChildTimeoutTelemetry) -> None: ...


class PiCompletionProfile:
    """Own Pi precedence, deadlines, nudges, and lifecycle phases."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        session_role: str,
        child_wave_timeout_seconds: float | None,
        emit_phase: EmitPiPhase,
        send_done_nudge: SendPiDoneNudge | None,
        evidence: PiCompletionEvidenceView,
        private_work_ledger: PiPrivateWorkLedger,
        stabilization_seconds: float,
        clock: Callable[[], float],
    ) -> None:
        self.runtime_root = runtime_root
        self.spawn_id = spawn_id
        self.session_role = session_role
        self.child_wave_timeout_seconds = child_wave_timeout_seconds
        self.emit_phase = emit_phase
        self.send_done_nudge_callback = send_done_nudge
        self.evidence = evidence
        self._private_work_ledger = private_work_ledger
        self._stabilization_seconds = stabilization_seconds
        self.tracker = evidence.tracker
        self.quiescence_tracker = evidence.quiescence_tracker
        self._clock = clock
        self.quiescence_enabled = False
        self.last_successful_terminal: TerminalEventOutcome | None = None
        self.micro_drain_active = False
        self.micro_drain_event_count = 0
        self.waiting_work_signature: tuple[int, int] | None = None
        self.waiting_notification_count: int | None = None
        self.child_wave_deadline_monotonic: float | None = None
        self.child_wave_started_monotonic: float | None = None
        self.done_nudge_idle_delay_seconds = PI_DONE_NUDGE_IDLE_DELAY_SECONDS
        self.done_nudge_interval_seconds = COMPLETION_NUDGE_INTERVAL_SECONDS
        self.next_done_nudge_monotonic: float | None = None
        self._done_requested = False
        self._awaiting_readable_evidence = False
        self._notification_timeout_error: str | None = None
        self._cleanup: PiCompletionCleanupPort | None = None

    def bind_cleanup(self, cleanup: PiCompletionCleanupPort) -> None:
        self._cleanup = cleanup

    def start(self) -> None:
        self._emit("drain_started")

    def stop(self) -> None:
        self._clear_done_nudge_timer()
        self._awaiting_readable_evidence = False

    def emit(self, phase: str, **payload: object) -> None:
        self._emit(phase, **payload)

    def set_policy(self, policy: DrainPolicy) -> None:
        self.quiescence_enabled = isinstance(policy, PiRpcQuiescenceDrainPolicy)

    def consume_directives(
        self,
        state: CompletionState,
        trigger: AssessmentTrigger,
    ) -> CompletionDirectives:
        if (
            trigger in {"timeout", "evidence_due"}
            and state.phase != "stabilizing"
            and self.last_successful_terminal is not None
            and not self._done_requested
        ):
            self._done_requested = consume_resident_signals(
                self.runtime_root, self.spawn_id
            ).done
        return CompletionDirectives(done=self._done_requested)

    def evaluate(self, context: CompletionEvaluation) -> ProfileDecision:
        lifecycle_failure = context.evidence_failure
        if lifecycle_failure is not None:
            return ProfileDecision(
                action="fail",
                outcome=_terminal_outcome(
                    status="failed",
                    exit_code=1,
                    error=lifecycle_failure.detail or lifecycle_failure.code,
                ),
            )
        if context.terminal_action is not None:
            return self._evaluate_terminal(context)
        if context.trigger == "event":
            failure = self.failure_outcome_after_event()
            if failure is not None:
                return ProfileDecision(action="fail", outcome=failure)
        if context.state.phase == "stabilizing":
            return self._evaluate_stabilizing(context)
        if context.trigger == "timeout" or (
            context.trigger == "evidence_due"
            and (
                context.directives.done
                or context.deadline_expired
                or context.profile_timer_due
            )
        ):
            return self._evaluate_timeout(context)
        candidate = context.candidate or self.last_successful_terminal
        if context.directives.done and candidate is not None:
            return self._evaluate_done(context, candidate)
        if (
            self.quiescence_enabled
            and (context.candidate is not None or self.last_successful_terminal is not None)
            and context.assessment.disposition == "ready"
        ):
            self._start_micro_drain()
            return ProfileDecision(
                action="stabilize",
                restart_stabilization=context.evidence_activity is not None,
                candidate=(
                    self.last_successful_terminal if context.candidate is None else None
                ),
            )
        return ProfileDecision(action="wait", reset_deadline=True)

    def deadline_for(self, decision: ProfileDecision, now: float) -> float | None:
        if not self.quiescence_enabled or decision.action not in {
            "wait",
            "stabilize",
            "abandon_candidate",
        }:
            return None
        unreadable_deadline = None
        if self._awaiting_readable_evidence and decision.action == "wait":
            timeout_seconds = self.child_wave_timeout_seconds
            if timeout_seconds is None or timeout_seconds <= 0:
                timeout_seconds = PI_EVIDENCE_UNREADABLE_TIMEOUT_SECONDS
            unreadable_deadline = now + timeout_seconds
        notification_deadline = self._next_notification_deadline()
        deadlines = [
            deadline
            for deadline in (
                unreadable_deadline,
                notification_deadline,
                self._active_child_wave_deadline(),
            )
            if deadline is not None
        ]
        return min(deadlines) if deadlines else None

    def stabilization_seconds(self) -> float:
        return self._stabilization_seconds

    def close_outcome(
        self, state: CompletionState, intentional_stop: bool
    ) -> TerminalEventOutcome | None:
        del intentional_stop
        return state.candidate if self.micro_drain_active else None

    def next_nudge_at(
        self, state: CompletionState, assessment: WorkAssessment
    ) -> float | None:
        del state, assessment
        self._refresh_done_nudge_state()
        return self.next_done_nudge_monotonic

    async def send_nudge(self, urgency: NudgeUrgency) -> None:
        del urgency
        try:
            if self.send_done_nudge_callback is not None:
                await self.send_done_nudge_callback(PI_COMPLETION_NUDGE_MESSAGE)
        except Exception:
            return

    def stream_exit_decision(
        self,
        state: CompletionState,
        recorded_outcome: TerminalEventOutcome | None,
    ) -> ProfileExitDecision:
        del state
        pending_children = self.pending_children_at_exit()
        if pending_children and recorded_outcome is None:
            recorded_outcome = _terminal_outcome(
                status="failed",
                exit_code=1,
                error="pi_process_exited_with_tracked_children",
            )
        return ProfileExitDecision(
            recorded_outcome=recorded_outcome,
            fallback_error=self.fallback_error_without_recorded_outcome(),
            cleanup_reason=(
                "pi_process_exit_with_tracked_children" if pending_children else None
            ),
        )

    def note_persisted_activity(self, event: HarnessEvent) -> EvidenceActivity | None:
        if not self.quiescence_enabled or not self.micro_drain_active:
            return None
        self.micro_drain_event_count += 1
        self.micro_drain_active = False
        self._emit(
            "quiescence_micro_drain_extended",
            event_type=event.event_type,
            micro_drain_events=self.micro_drain_event_count,
        )
        return EvidenceActivity(code="pi_micro_drain_extended")

    def observe_terminal_event(
        self,
        event: HarnessEvent,
        outcome: TerminalEventOutcome,
    ) -> None:
        if outcome.status != "succeeded":
            return
        self.last_successful_terminal = outcome
        completed_notification_id = self.tracker.resolve_notification_on_terminal(event)
        if completed_notification_id is not None:
            self._emit("continuation_completed", notification_id=completed_notification_id)
        self._refresh_done_nudge_state()
        self.emit_waiting_phases_if_needed()

    def after_observed_event(self, transition: str | None) -> None:
        if transition == "turn_active":
            self._clear_child_wave_timer()
        elif transition == "idle":
            self._update_idle_waiting_state()
        if not self.evidence.has_pending_children():
            self._clear_child_wave_timer()
        self._refresh_done_nudge_state()
        self.emit_waiting_phases_if_needed()

    def after_disk_change(self) -> None:
        if not self.quiescence_enabled or not self.quiescence_tracker.parent_idle:
            return
        self._update_idle_waiting_state()

    def failure_outcome_after_event(self) -> TerminalEventOutcome | None:
        if (
            self.tracker.notification_failure_error is not None
            and self.quiescence_tracker.parent_idle
            and not self.evidence.has_pending_children()
        ):
            return _terminal_outcome(
                status="failed",
                exit_code=1,
                error=self.tracker.notification_failure_error,
            )
        if self._notification_timeout_error is not None:
            return _terminal_outcome(
                status="failed",
                exit_code=1,
                error=self._notification_timeout_error,
            )
        return None

    def pending_children_at_exit(self) -> bool:
        return self.quiescence_enabled and self.evidence.has_pending_children()

    def fallback_error_without_recorded_outcome(self) -> str | None:
        return self.tracker.notification_failure_error

    def after_finalized(
        self,
        *,
        connection_session_id: str | None,
        outcome: DrainOutcome,
    ) -> None:
        session_id = connection_session_id if self.evidence.session_seen else None
        if not self.evidence.session_phase_emitted:
            self._emit(
                "session_event_seen" if self.evidence.session_seen else "session_event_absent",
                session_id=session_id,
            )
        self._emit(
            "finalized",
            status=outcome.status,
            exit_code=outcome.exit_code,
            error=outcome.error,
        )

    def classify_outstanding_work(self) -> PiOutstandingWork:
        from meridian.lib.state import spawn_store
        from meridian.lib.state.spawn.model import SpawnRecord
        from meridian.lib.state.spawn_tree import (
            has_outstanding_descendant_work,
            iter_descendants_from_parent_map,
        )

        private_work = self.quiescence_tracker.private_work_snapshot()
        try:
            rows = spawn_store.list_spawns(self.runtime_root)
        except Exception:
            return PiOutstandingWork(
                spawn_children=True,
                non_spawn_processes=private_work.tracked_bash_bg
                or bool(private_work.rowless_subspawn_ids),
            )
        by_parent: dict[str | None, list[SpawnRecord]] = {}
        for row in rows:
            by_parent.setdefault(row.parent_id, []).append(row)
        active_descendant_ids = {
            child.id
            for child in iter_descendants_from_parent_map(str(self.spawn_id), by_parent)
            if is_active_spawn_status(child.status)
        }
        rowless_tracked_subspawn_ids = (
            set(private_work.rowless_subspawn_ids) - active_descendant_ids
        )
        rowless_meridian_spawn_ids = {
            subspawn_id
            for subspawn_id in rowless_tracked_subspawn_ids
            if spawn_store.is_spawn_id_shape(subspawn_id)
        }
        rowless_pi_internal_subspawns = bool(
            rowless_tracked_subspawn_ids - rowless_meridian_spawn_ids
        )
        return PiOutstandingWork(
            spawn_children=has_outstanding_descendant_work(str(self.spawn_id), rows),
            unknown_spawn_children=bool(rowless_meridian_spawn_ids),
            non_spawn_processes=(
                private_work.tracked_bash_bg or rowless_pi_internal_subspawns
            ),
        )

    def emit_waiting_phases_if_needed(self) -> None:
        if not self.quiescence_enabled:
            return
        private_work = self.quiescence_tracker.private_work_snapshot()
        waiting_for_children = self.evidence.has_pending_children()
        child_count = self.evidence.pending_child_count()
        rowless_count = len(private_work.rowless_subspawn_ids)
        waiting_signature = (child_count, rowless_count)
        if waiting_for_children:
            if waiting_signature != self.waiting_work_signature:
                self._emit(
                    "waiting_for_tracked_children",
                    active_tracked_count=child_count,
                    persisted_descendant_count=max(0, child_count - rowless_count),
                    rowless_subspawn_count=rowless_count,
                )
            self.waiting_work_signature = waiting_signature
        else:
            self.waiting_work_signature = None

        waiting_for_notifications = (
            not waiting_for_children and bool(private_work.pending_notifications)
        )
        notification_count = len(private_work.pending_notifications)
        if waiting_for_notifications:
            if notification_count != self.waiting_notification_count:
                self._emit(
                    "waiting_for_notification_completion",
                    pending_notification_count=notification_count,
                )
            self.waiting_notification_count = notification_count
        else:
            self.waiting_notification_count = None

    def _evaluate_terminal(self, context: CompletionEvaluation) -> ProfileDecision:
        action = context.terminal_action
        outcome = context.terminal_outcome
        assert action is not None
        assert outcome is not None
        if not action.terminate:
            return ProfileDecision(action="wait", emit_turn_boundary=action.emit_turn_boundary)
        if not self.quiescence_enabled or outcome.status != "succeeded":
            self._clear_done_nudge_timer()
            return ProfileDecision(
                action="complete" if outcome.status == "succeeded" else "fail",
                outcome=outcome,
            )
        if context.assessment.disposition == "ready":
            self._start_micro_drain()
            return ProfileDecision(action="stabilize")
        self.micro_drain_active = False
        private_work = self.quiescence_tracker.private_work_snapshot()
        active_tracked_count = self.evidence.pending_child_count()
        rowless_count = len(private_work.rowless_subspawn_ids)
        self._emit(
            "quiescence_deferred",
            active_tracked_count=active_tracked_count,
            persisted_descendant_count=max(0, active_tracked_count - rowless_count),
            pending_notification_count=len(private_work.pending_notifications),
            rowless_subspawn_count=rowless_count,
            tracked_bash_count=int(private_work.tracked_bash_bg),
            disk_notification_count=int(private_work.pending_disk_notification),
        )
        return ProfileDecision(action="wait", reset_deadline=True)

    def _evaluate_stabilizing(self, context: CompletionEvaluation) -> ProfileDecision:
        candidate = context.candidate or self.last_successful_terminal
        assert candidate is not None
        if context.evidence_activity is not None:
            if context.assessment.disposition == "ready":
                self._start_micro_drain()
                return ProfileDecision(action="stabilize", restart_stabilization=True)
            self.micro_drain_active = False
            self._update_idle_waiting_state()
            return ProfileDecision(action="wait", reset_deadline=True)
        if context.trigger == "aux_wake":
            if context.assessment.disposition != "ready":
                self._update_idle_waiting_state()
            return ProfileDecision(action="hold_stabilization")
        if context.trigger == "timeout" or (
            context.trigger == "evidence_due" and context.stabilization_elapsed
        ):
            if context.assessment.disposition == "ready":
                self.micro_drain_active = False
                self._clear_done_nudge_timer()
                return ProfileDecision(action="complete", outcome=candidate)
            self.micro_drain_active = False
            self._emit("quiescence_micro_drain_cancelled", reason="disk_state_changed")
            self._update_idle_waiting_state()
            return ProfileDecision(action="abandon_candidate", reset_deadline=True)
        return ProfileDecision(action="stabilize")

    def _evaluate_timeout(self, context: CompletionEvaluation) -> ProfileDecision:
        candidate = context.candidate or self.last_successful_terminal
        if context.directives.done and candidate is not None:
            return self._evaluate_done(context, candidate)

        expired_notification = self._private_work_ledger.pop_expired_notification(
            context.now
        )
        if expired_notification is not None:
            return ProfileDecision(
                action="fail",
                outcome=self._notification_timeout_outcome(expired_notification, context.now),
            )

        if self._child_wave_timed_out(context.now):
            self._prepare_child_timeout(context.now)
            return ProfileDecision(
                action="cleanup",
                outcome=_terminal_outcome(
                    status="failed",
                    exit_code=1,
                    error=_pi_child_wave_timeout_error(),
                ),
                cleanup_reason="pi_child_wave_timeout",
            )

        if context.profile_timer_due and self._done_nudge_due(context.now):
            self.next_done_nudge_monotonic = (
                context.now + self.done_nudge_interval_seconds
            )
            return ProfileDecision(action="wait", nudge="normal", reset_deadline=True)
        return ProfileDecision(action="wait", reset_deadline=True)

    def _evaluate_done(
        self,
        context: CompletionEvaluation,
        candidate: TerminalEventOutcome,
    ) -> ProfileDecision:
        self._clear_done_nudge_timer()
        if context.assessment.disposition != "unknown":
            self._awaiting_readable_evidence = False
            return ProfileDecision(action="complete", outcome=candidate)
        if context.deadline_expired:
            self._awaiting_readable_evidence = False
            return ProfileDecision(
                action="fail",
                outcome=_terminal_outcome(
                    status="failed",
                    exit_code=1,
                    error="pi_evidence_unreadable",
                ),
            )
        self._awaiting_readable_evidence = True
        return ProfileDecision(action="wait")

    def _start_micro_drain(self) -> None:
        self.micro_drain_active = True
        self.micro_drain_event_count = 0
        self._clear_done_nudge_timer()
        self._emit("quiescence_micro_drain_started")

    def _update_idle_waiting_state(self) -> None:
        if (
            self.child_wave_timeout_seconds is not None
            and self.child_wave_timeout_seconds > 0
            and self.evidence.has_pending_children()
            and self.child_wave_deadline_monotonic is None
        ):
            wave_start = self._clock()
            self.child_wave_started_monotonic = wave_start
            self.child_wave_deadline_monotonic = (
                wave_start + self.child_wave_timeout_seconds
            )
        if not self.evidence.has_pending_children():
            self._clear_child_wave_timer()
        self._refresh_done_nudge_state()
        self.emit_waiting_phases_if_needed()

    def _refresh_done_nudge_state(self) -> None:
        if (
            not self.quiescence_enabled
            or self.last_successful_terminal is None
            or not self.quiescence_tracker.parent_idle
            or self.micro_drain_active
        ):
            self._clear_done_nudge_timer()
            return
        outstanding = self.classify_outstanding_work()
        if (
            outstanding.spawn_children
            or outstanding.unknown_spawn_children
            or not outstanding.non_spawn_processes
        ):
            self._clear_done_nudge_timer()
            return
        if self.next_done_nudge_monotonic is None:
            self.next_done_nudge_monotonic = self._clock() + max(
                0.0, self.done_nudge_idle_delay_seconds
            )

    def _done_nudge_due(self, now: float) -> bool:
        return (
            self.next_done_nudge_monotonic is not None
            and now >= self.next_done_nudge_monotonic
        )

    def _notification_timeout_outcome(
        self,
        expired_notification: PiPendingNotification,
        now: float,
    ) -> TerminalEventOutcome:
        timeout_error = _pi_notification_timeout_error(
            expired_notification,
            now_monotonic=now,
        )
        self._notification_timeout_error = timeout_error
        self._emit(
            "pi_notification_timeout",
            notification_id=expired_notification.notification_id,
            notification_phase=expired_notification.phase,
            timeout_seconds=(
                expired_notification.deadline_monotonic
                - expired_notification.started_monotonic
                if expired_notification.deadline_monotonic is not None
                else None
            ),
        )
        return _terminal_outcome(status="failed", exit_code=1, error=timeout_error)

    def _prepare_child_timeout(self, now: float) -> None:
        cleanup = self._cleanup
        assert cleanup is not None
        if cleanup.terminate_children is None:
            raise RuntimeError("Pi child timeout cleanup is not configured")
        elapsed_seconds = 0.0
        if self.child_wave_started_monotonic is not None:
            elapsed_seconds = max(0.0, now - self.child_wave_started_monotonic)
        timeout_seconds = 0.0
        if (
            self.child_wave_deadline_monotonic is not None
            and self.child_wave_started_monotonic is not None
        ):
            timeout_seconds = max(
                0.0,
                self.child_wave_deadline_monotonic
                - self.child_wave_started_monotonic,
            )
        cleanup.prepare_child_timeout(
            ChildTimeoutTelemetry(
                active_tracked_count=self.evidence.pending_child_count(),
                elapsed_seconds=elapsed_seconds,
                timeout_seconds=timeout_seconds,
            )
        )
        self._clear_child_wave_timer()

    def _active_child_wave_deadline(self) -> float | None:
        if (
            self.child_wave_deadline_monotonic is None
            or not self.quiescence_tracker.parent_idle
            or not self.evidence.has_pending_children()
        ):
            return None
        return self.child_wave_deadline_monotonic

    def _child_wave_timed_out(self, now: float) -> bool:
        deadline = self._active_child_wave_deadline()
        return deadline is not None and now >= deadline

    def _next_notification_deadline(self) -> float | None:
        deadlines = [
            item.deadline_monotonic
            for item in self.quiescence_tracker.private_work_snapshot().pending_notifications
            if item.deadline_monotonic is not None
        ]
        return min(deadlines) if deadlines else None

    def _clear_child_wave_timer(self) -> None:
        self.child_wave_deadline_monotonic = None
        self.child_wave_started_monotonic = None

    def _clear_done_nudge_timer(self) -> None:
        self.next_done_nudge_monotonic = None

    def _emit(self, phase: str, **payload: object) -> None:
        self.emit_phase(phase=phase, session_role=self.session_role or None, **payload)



def _pi_notification_timeout_error(
    pending: PiPendingNotification,
    *,
    now_monotonic: float,
) -> str:
    timeout_seconds = 0.0
    if pending.deadline_monotonic is not None:
        timeout_seconds = max(0.0, pending.deadline_monotonic - pending.started_monotonic)
    elapsed_seconds = max(0.0, now_monotonic - pending.started_monotonic)
    return (
        "pi_notification_timeout:"
        f"id={pending.notification_id}:"
        f"phase={pending.phase}:"
        f"elapsed={elapsed_seconds:.3f}:"
        f"timeout={timeout_seconds:.3f}"
    )


def _pi_child_wave_timeout_error() -> str:
    return "pi_child_wave_timeout"


def _terminal_outcome(
    *,
    status: SpawnStatus,
    exit_code: int,
    error: str | None,
) -> TerminalEventOutcome:
    from meridian.lib.harness.semantics import TerminalEventOutcome

    return TerminalEventOutcome(status=status, exit_code=exit_code, error=error)


__all__ = ["ChildTimeoutTelemetry", "PiCompletionProfile", "PiOutstandingWork"]
