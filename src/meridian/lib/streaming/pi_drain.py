"""Pi completion collaborators and their drain construction wrapper."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.core.types import SpawnId
from meridian.lib.harness import pi_lifecycle_events as pi_lifecycle
from meridian.lib.state.spawn_signals import consume_resident_signals
from meridian.lib.streaming.completion_contracts import (
    AssessmentTrigger,
    CleanupReport,
    CompletionDirectives,
    CompletionEvaluation,
    CompletionState,
    DiagnosticBlocker,
    EvidenceActivity,
    EvidenceEventDecision,
    EvidenceFailure,
    NudgeUrgency,
    ProfileDecision,
    WorkAssessment,
)
from meridian.lib.streaming.completion_coordinator import CompletionCoordinator
from meridian.lib.streaming.completion_nudge import (
    COMPLETION_NUDGE_INTERVAL_SECONDS,
    PI_COMPLETION_NUDGE_MESSAGE,
)
from meridian.lib.streaming.drain_coordinator import (
    DrainExitDecision,
    DrainLoopDecision,
    DrainTerminalDecision,
)
from meridian.lib.streaming.drain_policy import (
    DrainAction,
    DrainPolicy,
    PiRpcQuiescenceDrainPolicy,
)
from meridian.lib.streaming.pi_quiescence import PiQuiescenceTracker
from meridian.lib.streaming.pi_subspawn_tracker import PiPendingNotification, PiSubspawnTracker

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessConnection, HarnessEvent
    from meridian.lib.harness.semantics import TerminalEventOutcome
    from meridian.lib.launch.launch_types import ResolvedLaunchSpec
    from meridian.lib.streaming.spawn_session import DrainOutcome

_PI_PHASE_EVENT_TYPE = pi_lifecycle.PI_PHASE_EVENT_TYPE
_PENDING_DISK_POLL_INTERVAL_SECONDS = 0.25
logger = logging.getLogger(__name__)

PI_MICRO_DRAIN_TIMEOUT_SECONDS: float = 0.05
PI_DONE_NUDGE_IDLE_DELAY_SECONDS: float = 5.0

EmitPiPhase = Callable[..., None]
TerminatePiChildren = Callable[[PiSubspawnTracker, str], Awaitable[None]]
SendPiDoneNudge = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class PiOutstandingWork:
    """Classify Pi work that remains after the parent goes idle."""

    spawn_children: bool
    non_spawn_processes: bool
    unknown_spawn_children: bool = False


class _PiCompletionEvidence:
    """Delegate Pi readiness to the current tracker and disk-backed authority."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        tracker: PiSubspawnTracker,
        quiescence_tracker: PiQuiescenceTracker,
        notification_timeout_seconds: float | None,
        clock: Callable[[], float],
    ) -> None:
        self.runtime_root = runtime_root
        self.spawn_id = spawn_id
        self.tracker = tracker
        self.quiescence_tracker = quiescence_tracker
        self.notification_timeout_seconds = notification_timeout_seconds
        self._clock = clock
        self._profile: _PiCompletionProfile | None = None
        self._generation = 0
        self._last_signature: object = None
        self._next_due_at: float | None = None
        self.session_seen = False
        self.session_phase_emitted = False

    def bind_profile(self, profile: _PiCompletionProfile) -> None:
        self._profile = profile

    async def start(self) -> None:
        await self.quiescence_tracker.start()

    async def stop(self) -> None:
        self._next_due_at = None
        await self.quiescence_tracker.stop()

    async def observe_event(
        self, event: HarnessEvent, transition: str | None
    ) -> EvidenceEventDecision:
        duplicate = self.tracker.observe(
            event,
            now_monotonic=self._clock(),
            notification_timeout_seconds=self.notification_timeout_seconds,
        )
        if duplicate:
            return EvidenceEventDecision(duplicate_canonical_event=True)
        if event.event_type == "session":
            self.session_seen = True
        if event.event_type == _PI_PHASE_EVENT_TYPE:
            phase_value = event.payload.get("phase")
            if phase_value in {"session_event_seen", "session_event_absent"}:
                self.session_phase_emitted = True
                if phase_value == "session_event_seen":
                    self.session_seen = True
        if transition == "turn_active":
            self.quiescence_tracker.mark_turn_active()
        elif transition == "idle":
            await self.quiescence_tracker.mark_idle()
        return EvidenceEventDecision()

    def note_event_persisted(self, event: HarnessEvent) -> EvidenceEventDecision:
        profile = self._profile
        activity = profile.note_persisted_activity(event) if profile is not None else None
        lifecycle_error = self.tracker.lifecycle_tracking_invalidated_error
        failure = (
            EvidenceFailure(code="pi_lifecycle_tracking_invalidated", detail=lifecycle_error)
            if lifecycle_error is not None
            else None
        )
        if activity is None and self.tracker.notification_failure_error is not None:
            activity = EvidenceActivity(code="pi_notification_failure")
        return EvidenceEventDecision(activity=activity, failure=failure)

    async def assess(self, trigger: AssessmentTrigger) -> WorkAssessment:
        profile = self._profile
        if trigger == "aux_wake" or (
            trigger == "timeout" and profile is not None and profile.micro_drain_active
        ):
            await self.quiescence_tracker.refresh_disk_state()
        store_failure = self._store_read_failure()
        if store_failure is not None:
            return WorkAssessment(
                disposition="unknown",
                blockers=(),
                generation=self._next_generation(("unknown", store_failure.detail)),
                failure=store_failure,
            )
        try:
            blockers = self._blockers()
        except Exception as exc:
            failure = EvidenceFailure(code="pi_store_read_failed", detail=str(exc))
            assessment = WorkAssessment(
                disposition="unknown",
                blockers=(),
                generation=self._next_generation(("unknown", failure.detail)),
                failure=failure,
            )
        else:
            signature = tuple((item.code, item.identity) for item in blockers)
            assessment = (
                WorkAssessment(
                    disposition="blocked",
                    blockers=blockers,
                    generation=self._next_generation(signature),
                )
                if blockers
                else WorkAssessment(
                    disposition="ready",
                    blockers=(),
                    generation=self._next_generation(signature),
                )
            )
        self._next_due_at = (
            self._clock() + _PENDING_DISK_POLL_INTERVAL_SECONDS
            if self.quiescence_tracker.has_pending_child_spawns()
            else None
        )
        return assessment

    def next_due_at(self) -> float | None:
        return self._next_due_at

    async def handle_due(self) -> EvidenceEventDecision:
        self._next_due_at = None
        await self.quiescence_tracker.refresh_disk_state()
        return EvidenceEventDecision()

    def wants_aux_wake(self) -> bool:
        profile = self._profile
        return bool(profile is not None and profile.quiescence_enabled)

    async def wait_for_change(self) -> None:
        await self.quiescence_tracker.wait_for_disk_change()

    async def refresh_disk_state(self) -> None:
        await self.quiescence_tracker.refresh_disk_state()

    def is_quiescent(self) -> bool:
        return (
            self.quiescence_tracker.is_quiescent()
            and not self.tracker.has_pending()
            and not self.tracker.has_pending_notifications()
        )

    def has_pending_children(self) -> bool:
        return self.tracker.has_pending() or self.quiescence_tracker.has_pending_child_spawns()

    def pending_child_count(self) -> int:
        return (
            self.tracker.active_tracked_count()
            + self.quiescence_tracker.pending_child_spawn_count()
        )

    def _blockers(self) -> tuple[DiagnosticBlocker, ...]:
        blockers: list[DiagnosticBlocker] = []
        if not self.quiescence_tracker.parent_idle:
            blockers.append(DiagnosticBlocker(source="profile", code="pi_parent_active"))
        blockers.extend(
            DiagnosticBlocker(
                source="persisted_descendant",
                code="pi_tracked_child",
                identity=child_id,
            )
            for child_id in sorted(self.tracker.active_ids)
        )
        disk_child_count = self.quiescence_tracker.pending_child_spawn_count()
        blockers.extend(
            DiagnosticBlocker(source="persisted_descendant", code="pi_direct_child")
            for _ in range(disk_child_count)
        )
        if self.quiescence_tracker.has_tracked_bash_bg():
            blockers.append(DiagnosticBlocker(source="profile", code="pi_tracked_bash_bg"))
        pending_notifications = self.tracker.pending_notifications or {}
        blockers.extend(
            DiagnosticBlocker(
                source="profile",
                code="pi_pending_notification",
                identity=notification_id,
            )
            for notification_id in sorted(pending_notifications)
        )
        if not blockers and not self.quiescence_tracker.is_quiescent():
            blockers.append(DiagnosticBlocker(source="profile", code="pi_disk_notification"))
        return tuple(blockers)

    def _store_read_failure(self) -> EvidenceFailure | None:
        from meridian.lib.state import spawn_store

        try:
            spawn_store.list_spawns(self.runtime_root)
        except ValidationError:
            # Pi's current raw direct-child authority admits partial legacy rows.
            # Invalid rows therefore remain absent from this health probe in Phase 1.
            return None
        except Exception as exc:
            return EvidenceFailure(code="pi_store_read_failed", detail=str(exc))
        return None

    def _next_generation(self, signature: object) -> int:
        if signature != self._last_signature:
            self._generation += 1
            self._last_signature = signature
        return self._generation


@dataclass(frozen=True)
class _ChildTimeoutTelemetry:
    active_tracked_count: int
    elapsed_seconds: float
    timeout_seconds: float


class _PiCompletionProfile:
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
        evidence: _PiCompletionEvidence,
        clock: Callable[[], float],
    ) -> None:
        self.runtime_root = runtime_root
        self.spawn_id = spawn_id
        self.session_role = session_role
        self.child_wave_timeout_seconds = child_wave_timeout_seconds
        self.emit_phase = emit_phase
        self.send_done_nudge_callback = send_done_nudge
        self.evidence = evidence
        self.tracker = evidence.tracker
        self.quiescence_tracker = evidence.quiescence_tracker
        self._clock = clock
        self.quiescence_enabled = False
        self.last_successful_terminal: TerminalEventOutcome | None = None
        self.micro_drain_active = False
        self.micro_drain_event_count = 0
        self.waiting_child_count: int | None = None
        self.waiting_notification_count: int | None = None
        self.child_wave_deadline_monotonic: float | None = None
        self.child_wave_started_monotonic: float | None = None
        self.done_nudge_idle_delay_seconds = PI_DONE_NUDGE_IDLE_DELAY_SECONDS
        self.done_nudge_interval_seconds = COMPLETION_NUDGE_INTERVAL_SECONDS
        self.next_done_nudge_monotonic: float | None = None
        self._done_requested = False
        self._consume_done_enabled = False
        self._cleanup: _PiCompletionCleanup | None = None

    def bind_cleanup(self, cleanup: _PiCompletionCleanup) -> None:
        self._cleanup = cleanup

    def start(self) -> None:
        self._emit("drain_started")

    def stop(self) -> None:
        self._clear_done_nudge_timer()

    def emit(self, phase: str, **payload: object) -> None:
        self._emit(phase, **payload)

    def set_policy(self, policy: DrainPolicy) -> None:
        self.quiescence_enabled = isinstance(policy, PiRpcQuiescenceDrainPolicy)

    def enable_done_consumption(self, enabled: bool) -> None:
        self._consume_done_enabled = enabled

    def consume_directives(self) -> CompletionDirectives:
        if (
            self._consume_done_enabled
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
        if context.trigger in {"timeout", "evidence_due"}:
            return self._evaluate_timeout(context)
        if (
            self.quiescence_enabled
            and context.candidate is not None
            and context.assessment.disposition == "ready"
        ):
            self._start_micro_drain()
            return ProfileDecision(
                action="stabilize",
                restart_stabilization=context.evidence_activity is not None,
            )
        return ProfileDecision(action="wait", reset_deadline=True)

    def deadline_for(self, decision: ProfileDecision, now: float) -> float | None:
        del now
        if not self.quiescence_enabled or decision.action not in {"wait", "stabilize"}:
            return None
        notification_deadline = self._next_notification_deadline()
        deadlines = [
            deadline
            for deadline in (notification_deadline, self._active_child_wave_deadline())
            if deadline is not None
        ]
        return min(deadlines) if deadlines else None

    def stabilization_seconds(self) -> float:
        return PI_MICRO_DRAIN_TIMEOUT_SECONDS

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
        if self.tracker.notification_timeout_error is not None:
            return _terminal_outcome(
                status="failed",
                exit_code=1,
                error=self.tracker.notification_timeout_error,
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

        try:
            rows = spawn_store.list_spawns(self.runtime_root)
        except Exception:
            return PiOutstandingWork(
                spawn_children=True,
                non_spawn_processes=self.quiescence_tracker.has_tracked_bash_bg()
                or self.tracker.has_pending(),
            )
        by_parent: dict[str | None, list[SpawnRecord]] = {}
        for row in rows:
            by_parent.setdefault(row.parent_id, []).append(row)
        active_descendant_ids = {
            child.id
            for child in iter_descendants_from_parent_map(str(self.spawn_id), by_parent)
            if is_active_spawn_status(child.status)
        }
        rowless_tracked_subspawn_ids = self.tracker.active_ids - active_descendant_ids
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
            unknown_spawn_children=bool(rowless_meridian_spawn_ids)
            or self.quiescence_tracker.has_pending_child_spawns(),
            non_spawn_processes=(
                self.quiescence_tracker.has_tracked_bash_bg()
                or rowless_pi_internal_subspawns
            ),
        )

    def emit_waiting_phases_if_needed(self) -> None:
        if not self.quiescence_enabled:
            return
        waiting_for_children = self.evidence.has_pending_children()
        child_count = self.evidence.pending_child_count()
        if waiting_for_children:
            if child_count != self.waiting_child_count:
                self._emit("waiting_for_tracked_children", active_tracked_count=child_count)
            self.waiting_child_count = child_count
        else:
            self.waiting_child_count = None

        waiting_for_notifications = (
            not waiting_for_children and self.tracker.has_pending_notifications()
        )
        notification_count = self.tracker.pending_notification_count()
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
        self._emit(
            "quiescence_deferred",
            active_tracked_count=self.evidence.pending_child_count(),
            pending_notification_count=self.tracker.pending_notification_count(),
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
        if context.trigger == "timeout" or (
            context.trigger == "evidence_due" and context.stabilization_elapsed
        ):
            if (
                context.assessment.disposition == "ready"
                and context.assessment.generation == context.state.stabilization_generation
            ):
                self.micro_drain_active = False
                self._clear_done_nudge_timer()
                return ProfileDecision(action="complete", outcome=candidate)
            self.micro_drain_active = False
            self._emit("quiescence_micro_drain_cancelled", reason="disk_state_changed")
            self._update_idle_waiting_state()
            return ProfileDecision(action="wait", reset_deadline=True)
        return ProfileDecision(action="stabilize")

    def _evaluate_timeout(self, context: CompletionEvaluation) -> ProfileDecision:
        candidate = context.candidate or self.last_successful_terminal
        if context.directives.done and candidate is not None:
            self._clear_done_nudge_timer()
            return ProfileDecision(action="complete", outcome=candidate)

        expired_notification = self.tracker.pop_expired_notification(context.now)
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
        self.tracker.notification_timeout_error = timeout_error
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
            _ChildTimeoutTelemetry(
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
        pending = self.tracker.pending_notifications or {}
        deadlines = [
            item.deadline_monotonic
            for item in pending.values()
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


class _PiCompletionCleanup:
    """Terminate Pi-owned cleanup handles through the plan's canonical callback."""

    def __init__(
        self,
        *,
        tracker: PiSubspawnTracker,
        quiescence_tracker: PiQuiescenceTracker,
        terminate_children: TerminatePiChildren | None,
        emit_phase: Callable[..., None],
    ) -> None:
        self.tracker = tracker
        self.quiescence_tracker = quiescence_tracker
        self.terminate_children = terminate_children
        self.emit_phase = emit_phase
        self.tracked_cleanup_reason: str | None = None
        self.tracked_cleanup_error: str | None = None
        self._child_timeout_telemetry: _ChildTimeoutTelemetry | None = None

    def prepare_child_timeout(self, telemetry: _ChildTimeoutTelemetry) -> None:
        if self.tracked_cleanup_reason is None:
            self.tracked_cleanup_reason = "pi_child_wave_timeout"
        self._child_timeout_telemetry = telemetry

    async def cleanup(self, assessment: WorkAssessment, reason: str) -> CleanupReport:
        del assessment
        if reason != "pi_child_wave_timeout":
            return CleanupReport()
        callback = self.terminate_children
        handles_attempted = tuple(sorted(self.tracker.active_ids))
        failures: tuple[EvidenceFailure, ...] = ()
        try:
            if callback is None:
                raise RuntimeError("Pi child timeout cleanup is not configured")
            await callback(self.tracker, reason)
        except Exception as exc:
            self.tracked_cleanup_error = str(exc)
            failures = (EvidenceFailure(code="pi_child_cleanup_failed", detail=str(exc)),)
        finally:
            tracked_count = (
                self.tracker.clear_tracked_children_after_wave_timeout()
                + self.quiescence_tracker.pending_child_spawn_count()
            )
            telemetry = self._child_timeout_telemetry or _ChildTimeoutTelemetry(
                active_tracked_count=tracked_count,
                elapsed_seconds=0.0,
                timeout_seconds=0.0,
            )
            payload: dict[str, object] = {
                "active_tracked_count": tracked_count,
                "elapsed_seconds": telemetry.elapsed_seconds,
                "timeout_seconds": telemetry.timeout_seconds,
            }
            if self.tracked_cleanup_error is not None:
                payload["cleanup_error"] = self.tracked_cleanup_error
            try:
                self.emit_phase("pi_child_wave_timeout", **payload)
            except Exception:
                logger.warning("Failed to emit Pi child-wave timeout phase", exc_info=True)
        return CleanupReport(
            attempted_categories=("pi_tracked_children",),
            handles_attempted=handles_attempted,
            failures=failures,
        )

    async def cleanup_pending_children_at_exit(self) -> None:
        if not self.tracker.has_pending() or self.tracked_cleanup_reason is not None:
            return
        callback = self.terminate_children
        if callback is None:
            raise RuntimeError("Pi child process-exit cleanup is not configured")
        await callback(self.tracker, "pi_process_exit_with_tracked_children")
        self.tracked_cleanup_reason = "pi_process_exit_with_tracked_children"


class PiDrainCoordinator:
    """Thin compatibility wrapper constructing the shared completion coordinator."""

    def __init__(
        self,
        *,
        coordinator: CompletionCoordinator,
        evidence: _PiCompletionEvidence,
        profile: _PiCompletionProfile,
        cleanup: _PiCompletionCleanup,
    ) -> None:
        self._coordinator = coordinator
        self._evidence = evidence
        self._profile = profile
        self._cleanup = cleanup
        self.quiescence_tracker = evidence.quiescence_tracker
        self.tracker = evidence.tracker

    @classmethod
    def for_connection(
        cls,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        receiver: HarnessConnection[ResolvedLaunchSpec],
        session_role: str | None,
        notification_timeout_seconds: float | None,
        child_wave_timeout_seconds: float | None,
        emit_phase: EmitPiPhase,
        terminate_children: TerminatePiChildren | None = None,
        send_done_nudge: SendPiDoneNudge | None = None,
    ) -> PiDrainCoordinator:
        del receiver
        normalized_role = (session_role or "").strip().lower()
        clock = lambda: time.monotonic()  # noqa: E731 - preserve runtime clock patching
        tracker = PiSubspawnTracker.empty()
        quiescence_tracker = PiQuiescenceTracker.for_connection(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            is_pi_connection=True,
            session_role=normalized_role,
        )
        evidence = _PiCompletionEvidence(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            tracker=tracker,
            quiescence_tracker=quiescence_tracker,
            notification_timeout_seconds=notification_timeout_seconds,
            clock=clock,
        )
        profile = _PiCompletionProfile(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            session_role=normalized_role,
            child_wave_timeout_seconds=child_wave_timeout_seconds,
            emit_phase=emit_phase,
            send_done_nudge=send_done_nudge,
            evidence=evidence,
            clock=clock,
        )
        cleanup = _PiCompletionCleanup(
            tracker=tracker,
            quiescence_tracker=quiescence_tracker,
            terminate_children=terminate_children,
            emit_phase=profile.emit,
        )
        evidence.bind_profile(profile)
        profile.bind_cleanup(cleanup)
        coordinator = CompletionCoordinator(
            evidence=evidence,
            profile=profile,
            cleanup=cleanup,
            clock=clock,
            evaluate_without_candidate=True,
        )
        return cls(
            coordinator=coordinator,
            evidence=evidence,
            profile=profile,
            cleanup=cleanup,
        )

    @property
    def done_nudge_idle_delay_seconds(self) -> float:
        return self._profile.done_nudge_idle_delay_seconds

    @done_nudge_idle_delay_seconds.setter
    def done_nudge_idle_delay_seconds(self, value: float) -> None:
        self._profile.done_nudge_idle_delay_seconds = value

    @property
    def done_nudge_interval_seconds(self) -> float:
        return self._profile.done_nudge_interval_seconds

    @done_nudge_interval_seconds.setter
    def done_nudge_interval_seconds(self, value: float) -> None:
        self._profile.done_nudge_interval_seconds = value

    @property
    def last_successful_terminal(self) -> TerminalEventOutcome | None:
        return self._profile.last_successful_terminal

    @property
    def quiescence_candidate(self) -> TerminalEventOutcome | None:
        return self._coordinator.pending_outcome if self._profile.micro_drain_active else None

    @property
    def pending_outcome(self) -> TerminalEventOutcome | None:
        return self._coordinator.pending_outcome

    @pending_outcome.setter
    def pending_outcome(self, value: TerminalEventOutcome | None) -> None:
        self._coordinator.pending_outcome = value

    @property
    def deadline_monotonic(self) -> float | None:
        return self._coordinator.deadline_monotonic

    @deadline_monotonic.setter
    def deadline_monotonic(self, value: float | None) -> None:
        self._coordinator.deadline_monotonic = value

    async def start(self) -> None:
        await self._coordinator.start()
        self._profile.start()

    async def stop(self) -> None:
        self._profile.stop()
        await self._coordinator.stop()

    def set_policy(self, policy: DrainPolicy) -> None:
        self._profile.set_policy(policy)

    def next_timeout(self) -> float | None:
        if not self._profile.quiescence_enabled:
            return None
        if self._profile.micro_drain_active:
            return PI_MICRO_DRAIN_TIMEOUT_SECONDS
        return self._coordinator.next_timeout()

    async def observe_event(self, event: HarnessEvent, transition: str | None) -> bool:
        duplicate = await self._coordinator.observe_event(event, transition)
        if not duplicate:
            self._profile.after_observed_event(transition)
            self._sync_profile_deadline()
        return duplicate

    def note_event_persisted(self, event: HarnessEvent) -> DrainLoopDecision:
        micro_drain_active = self._profile.micro_drain_active
        decision = self._coordinator.note_event_persisted(event)
        if micro_drain_active and decision.recorded_outcome is None:
            self._coordinator.cancel_stabilization()
        return decision

    async def handle_terminal_event(
        self,
        event: HarnessEvent,
        outcome: TerminalEventOutcome,
        action: DrainAction,
    ) -> DrainTerminalDecision:
        self._profile.observe_terminal_event(event, outcome)
        decision = await self._coordinator.handle_terminal_event(event, outcome, action)
        if outcome.status == "succeeded" and not action.terminate:
            self._coordinator.pending_outcome = outcome
        self._sync_profile_deadline()
        return decision

    async def handle_timeout(
        self,
        terminate_children: TerminatePiChildren | None = None,
    ) -> DrainLoopDecision:
        if not self._profile.quiescence_enabled:
            raise TimeoutError
        prior_callback = self._cleanup.terminate_children
        if terminate_children is not None:
            self._cleanup.terminate_children = terminate_children
        self._profile.enable_done_consumption(
            self._coordinator.state.phase != "stabilizing"
        )
        try:
            return await self._coordinator.handle_timeout()
        finally:
            self._profile.enable_done_consumption(False)
            self._cleanup.terminate_children = prior_callback

    async def after_event(self) -> DrainLoopDecision:
        if self._coordinator.state.phase == "finalized":
            failure = self._profile.failure_outcome_after_event()
            return DrainLoopDecision(recorded_outcome=failure)
        decision = await self._coordinator.after_event()
        self._sync_profile_deadline()
        return decision

    def wants_aux_wake(self) -> bool:
        return self._coordinator.wants_aux_wake()

    async def wait_for_aux_wake(self) -> None:
        await self._coordinator.wait_for_aux_wake()

    async def handle_aux_wake(self) -> DrainLoopDecision:
        return await self.reevaluate_after_disk_change()

    async def reevaluate_after_disk_change(self) -> DrainLoopDecision:
        if not self._profile.quiescence_enabled:
            return DrainLoopDecision()
        if self._coordinator.state.phase == "stabilizing":
            await self._evidence.refresh_disk_state()
            self._profile.after_disk_change()
            self._sync_profile_deadline()
            return DrainLoopDecision()
        decision = await self._coordinator.handle_aux_wake()
        self._profile.after_disk_change()
        self._sync_profile_deadline()
        return decision

    def handle_close(self, *, intentional_stop: bool) -> TerminalEventOutcome | None:
        return self._coordinator.handle_close(intentional_stop=intentional_stop)

    async def handle_stream_exit(
        self,
        recorded_outcome: TerminalEventOutcome | None,
    ) -> DrainExitDecision:
        pending_children = self._profile.pending_children_at_exit()
        if pending_children:
            await self._cleanup.cleanup_pending_children_at_exit()
            if recorded_outcome is None:
                recorded_outcome = _terminal_outcome(
                    status="failed",
                    exit_code=1,
                    error="pi_process_exited_with_tracked_children",
                )
        return DrainExitDecision(
            recorded_outcome=recorded_outcome,
            fallback_error=self._profile.fallback_error_without_recorded_outcome(),
        )

    def after_finalized(
        self,
        *,
        connection_session_id: str | None,
        outcome: DrainOutcome,
    ) -> None:
        self._profile.after_finalized(
            connection_session_id=connection_session_id,
            outcome=outcome,
        )

    def is_quiescent(self) -> bool:
        return self._evidence.is_quiescent()

    def classify_outstanding_work(self) -> PiOutstandingWork:
        return self._profile.classify_outstanding_work()

    def _sync_profile_deadline(self) -> None:
        self._coordinator.deadline_monotonic = self._profile.deadline_for(
            ProfileDecision(action="wait"),
            time.monotonic(),
        )

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


__all__ = ["PiDrainCoordinator"]
