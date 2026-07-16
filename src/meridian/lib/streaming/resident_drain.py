"""Resident completion collaborators and their drain construction wrapper."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.liveness import LivenessDecision
from meridian.lib.harness.semantics import TerminalEventOutcome
from meridian.lib.state.spawn_signals import consume_resident_signals
from meridian.lib.streaming.completion_contracts import (
    AssessmentTrigger,
    CleanupReport,
    CompletionDirectives,
    CompletionEvaluation,
    CompletionState,
    DiagnosticBlocker,
    EvidenceEventDecision,
    EvidenceFailure,
    NudgeUrgency,
    ProfileDecision,
    ProfileExitDecision,
    WorkAssessment,
)
from meridian.lib.streaming.completion_coordinator import CompletionCoordinator
from meridian.lib.streaming.completion_nudge import (
    COMPLETION_NUDGE_INTERVAL_SECONDS,
    COMPLETION_NUDGE_MESSAGE,
    TIMEOUT_SOON_COMPLETION_NUDGE_MESSAGE,
)
from meridian.lib.streaming.descendant_evidence import ReconciledDescendantEvidence
from meridian.lib.streaming.drain_coordinator import (
    DrainExitDecision,
    DrainLoopDecision,
    DrainTerminalDecision,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from meridian.lib.harness.connections.base import HarnessConnection, HarnessEvent
    from meridian.lib.harness.connections.resident_backend import ResidentBackendControl
    from meridian.lib.streaming.drain_policy import DrainAction

logger = structlog.get_logger()


class _ResidentCompletionEvidence:
    """Assess the reconciled, transitive persisted descendant tree."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        poll_seconds: float,
        clock: Callable[[], float],
    ) -> None:
        self._poll_seconds = poll_seconds
        self._clock = clock
        self._descendant_evidence = ReconciledDescendantEvidence(
            runtime_root=runtime_root,
            root_spawn_id=spawn_id,
            blocker_reader=lambda: _outstanding_descendant_blockers(runtime_root, spawn_id),
        )
        self._next_due: float | None = None

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        self._next_due = None

    async def observe_event(
        self, event: HarnessEvent, transition: str | None
    ) -> EvidenceEventDecision:
        del event, transition
        return EvidenceEventDecision()

    def note_event_persisted(self, event: HarnessEvent) -> EvidenceEventDecision:
        del event
        return EvidenceEventDecision()

    async def assess(self, trigger: AssessmentTrigger) -> WorkAssessment:
        del trigger
        assessment = self._descendant_evidence.assess()
        self._next_due = self._clock() + self._poll_seconds
        return assessment

    def next_due_at(self) -> float | None:
        return self._next_due

    async def handle_due(self) -> EvidenceEventDecision:
        self._next_due = None
        return EvidenceEventDecision()

    def wants_aux_wake(self) -> bool:
        return False

    async def wait_for_change(self) -> None:
        return

class _ResidentCompletionProfile:
    """Own resident precedence, signals, backend state, deadline, and nudges."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        resident_backend: ResidentBackendControl,
        deadline_seconds: float,
        clock: Callable[[], float],
    ) -> None:
        self._runtime_root = runtime_root
        self._spawn_id = spawn_id
        self._resident_backend = resident_backend
        self._deadline_seconds = deadline_seconds
        self._clock = clock
        self._explicit_hold = False
        self._done_requested = False
        self._awaiting_done = False
        self._next_nudge_at: float | None = None

    def consume_directives(
        self,
        state: CompletionState,
        trigger: AssessmentTrigger,
    ) -> CompletionDirectives:
        del state, trigger
        signals = consume_resident_signals(self._runtime_root, self._spawn_id)
        self._done_requested = self._done_requested or signals.done
        return CompletionDirectives(done=self._done_requested, rearm=signals.rearm)

    def evaluate(self, context: CompletionEvaluation) -> ProfileDecision:
        if context.terminal_action is not None:
            return self._evaluate_terminal(context)
        return self._evaluate_wait(context)

    def deadline_for(self, decision: ProfileDecision, now: float) -> float | None:
        if decision.action not in {"wait", "stabilize"}:
            return None
        return now + self._deadline_seconds

    def stabilization_seconds(self) -> float:
        return 0.0

    def close_outcome(
        self, state: CompletionState, intentional_stop: bool
    ) -> TerminalEventOutcome | None:
        if state.candidate is None:
            return None
        if intentional_stop:
            return state.candidate
        if self._resident_health_status() == LivenessDecision.BACKEND_DEAD:
            return TerminalEventOutcome(
                status="failed",
                exit_code=1,
                error="backend_dead_while_awaiting_done",
            )
        return TerminalEventOutcome(
            status="failed",
            exit_code=1,
            error="stream_closed_while_awaiting_done",
        )

    def next_nudge_at(
        self, state: CompletionState, assessment: WorkAssessment
    ) -> float | None:
        del state, assessment
        return self._next_nudge_at if self._explicit_hold else None

    async def send_nudge(self, urgency: NudgeUrgency) -> None:
        self._next_nudge_at = self._clock() + COMPLETION_NUDGE_INTERVAL_SECONDS
        if self._resident_health_status() == LivenessDecision.BACKEND_DEAD:
            return
        message = (
            TIMEOUT_SOON_COMPLETION_NUDGE_MESSAGE
            if urgency == "timeout_soon"
            else COMPLETION_NUDGE_MESSAGE
        )
        await self._resident_backend.begin_followup_turn(message)

    def stream_exit_decision(
        self,
        state: CompletionState,
        recorded_outcome: TerminalEventOutcome | None,
    ) -> ProfileExitDecision:
        del state
        return ProfileExitDecision(recorded_outcome=recorded_outcome)

    def observe_activity_transition(self, transition: str | None) -> None:
        if transition != "turn_active":
            return
        self.set_awaiting_done(False)
        self._next_nudge_at = self._clock() + COMPLETION_NUDGE_INTERVAL_SECONDS

    def clear(self) -> None:
        self.set_awaiting_done(False)
        self._explicit_hold = False
        self._done_requested = False
        self._next_nudge_at = None

    def _evaluate_terminal(self, context: CompletionEvaluation) -> ProfileDecision:
        action = context.terminal_action
        outcome = context.terminal_outcome
        assert action is not None
        assert outcome is not None
        if not action.terminate:
            self.clear()
            return ProfileDecision(action="clear", emit_turn_boundary=action.emit_turn_boundary)
        if outcome.status != "succeeded":
            self.clear()
            return ProfileDecision(action="fail", outcome=outcome)
        if context.directives.rearm:
            self._mark_rearmed(context.now)
        if context.directives.done:
            if context.assessment.disposition == "unknown":
                self._enter_wait(context.now)
                return ProfileDecision(action="wait", emit_turn_boundary=True)
            self.clear()
            return ProfileDecision(action="complete", outcome=outcome)
        if context.assessment.disposition != "ready" or self._explicit_hold:
            self._enter_wait(context.now)
            return ProfileDecision(
                action="wait",
                emit_turn_boundary=True,
                reset_deadline=context.directives.rearm,
            )
        self.clear()
        return ProfileDecision(action="complete", outcome=outcome)

    def _evaluate_wait(self, context: CompletionEvaluation) -> ProfileDecision:
        candidate = context.candidate
        assert candidate is not None
        rearmed = context.directives.rearm
        if rearmed:
            self._mark_rearmed(context.now)
        if context.directives.done:
            if context.assessment.disposition == "unknown":
                if context.deadline_expired:
                    self.clear()
                    return ProfileDecision(
                        action="fail",
                        outcome=TerminalEventOutcome(
                            status="failed",
                            exit_code=1,
                            error="resident_evidence_unreadable",
                        ),
                    )
                return ProfileDecision(action="wait")
            self.clear()
            return ProfileDecision(action="complete", outcome=candidate)
        if context.deadline_expired and not rearmed:
            self.clear()
            return ProfileDecision(
                action="cleanup",
                outcome=TerminalEventOutcome(
                    status="timed_out",
                    exit_code=1,
                    error="resident_deadline_expired",
                ),
                cleanup_reason="resident_deadline_expired",
            )
        if context.active_turn:
            return ProfileDecision(action="wait", reset_deadline=rearmed)
        if context.assessment.disposition != "ready":
            return ProfileDecision(action="wait", reset_deadline=rearmed)
        if self._explicit_hold:
            nudge = (
                self._nudge_urgency(context)
                if context.profile_timer_due and not rearmed
                else None
            )
            return ProfileDecision(action="wait", nudge=nudge, reset_deadline=rearmed)
        self.clear()
        return ProfileDecision(action="complete", outcome=candidate)

    def _mark_rearmed(self, now: float) -> None:
        self._explicit_hold = True
        self._next_nudge_at = now + COMPLETION_NUDGE_INTERVAL_SECONDS

    def _enter_wait(self, now: float) -> None:
        if self._explicit_hold:
            self._next_nudge_at = now + COMPLETION_NUDGE_INTERVAL_SECONDS
        self.set_awaiting_done(True)

    def set_awaiting_done(self, awaiting_done: bool) -> None:
        self._resident_backend.set_awaiting_done(awaiting_done)
        self._awaiting_done = awaiting_done

    def _resident_health_status(self) -> LivenessDecision:
        try:
            return self._resident_backend.health_status()
        except Exception:
            return LivenessDecision.BACKEND_DEAD

    def _nudge_urgency(self, context: CompletionEvaluation) -> NudgeUrgency:
        deadline_at = context.state.deadline_at
        remaining = None if deadline_at is None else deadline_at - context.now
        if remaining is not None and remaining < COMPLETION_NUDGE_INTERVAL_SECONDS:
            return "timeout_soon"
        return "normal"


class _ResidentCompletionCleanup:
    """Cancel resident descendants through the canonical application service."""

    def __init__(self, *, project_root: Path, runtime_root: Path, spawn_id: SpawnId) -> None:
        self._project_root = project_root
        self._runtime_root = runtime_root
        self._spawn_id = spawn_id

    async def cleanup(self, assessment: WorkAssessment, reason: str) -> CleanupReport:
        del assessment
        from meridian.lib.bootstrap.services import build_spawn_application_service_from_roots

        try:
            service = build_spawn_application_service_from_roots(
                self._project_root, self._runtime_root
            )
            cancelled = await service.cancel_descendants(self._spawn_id)
        except Exception as exc:
            logger.exception(
                "Failed to terminate resident descendant spawns after deadline expiry.",
                spawn_id=str(self._spawn_id),
            )
            return CleanupReport(
                attempted_categories=("persisted_descendants",),
                failures=(EvidenceFailure(code=reason, detail=str(exc)),),
            )
        return CleanupReport(
            attempted_categories=("persisted_descendants",),
            converged_cancel_ids=frozenset(cancelled),
        )


class ResidentDrainCoordinator:
    """Thin compatibility wrapper constructing the shared completion coordinator."""

    def __init__(
        self,
        *,
        coordinator: CompletionCoordinator,
        profile: _ResidentCompletionProfile,
        deadline_seconds: float,
        poll_seconds: float,
    ) -> None:
        self._coordinator = coordinator
        self._profile = profile
        self.deadline_seconds = deadline_seconds
        self.poll_seconds = poll_seconds

    @classmethod
    def for_connection(
        cls,
        *,
        project_root: Path,
        runtime_root: Path,
        spawn_id: SpawnId,
        receiver: HarnessConnection[Any],
        resident_backend: ResidentBackendControl,
        deadline_seconds: float | None,
        poll_seconds: float | None,
    ) -> ResidentDrainCoordinator:
        del receiver
        resolved_deadline = (
            deadline_seconds if deadline_seconds and deadline_seconds > 0 else 3300.0
        )
        resolved_poll = poll_seconds if poll_seconds and poll_seconds > 0 else 5.0
        clock = time.monotonic
        profile = _ResidentCompletionProfile(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            resident_backend=resident_backend,
            deadline_seconds=resolved_deadline,
            clock=clock,
        )
        coordinator = CompletionCoordinator(
            evidence=_ResidentCompletionEvidence(
                runtime_root=runtime_root,
                spawn_id=spawn_id,
                poll_seconds=resolved_poll,
                clock=clock,
            ),
            profile=profile,
            cleanup=_ResidentCompletionCleanup(
                project_root=project_root,
                runtime_root=runtime_root,
                spawn_id=spawn_id,
            ),
            clock=clock,
        )
        return cls(
            coordinator=coordinator,
            profile=profile,
            deadline_seconds=resolved_deadline,
            poll_seconds=resolved_poll,
        )

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

    async def stop(self) -> None:
        self._profile.clear()
        await self._coordinator.stop()

    def observe_activity_transition(self, transition: str | None) -> None:
        self._profile.observe_activity_transition(transition)
        self._coordinator.note_activity_transition(transition)

    async def observe_event(self, event: HarnessEvent, transition: str | None) -> bool:
        self._profile.observe_activity_transition(transition)
        return await self._coordinator.observe_event(event, transition)

    def note_event_persisted(self, event: HarnessEvent) -> DrainLoopDecision:
        return self._coordinator.note_event_persisted(event)

    async def handle_terminal_event(
        self,
        event: HarnessEvent,
        outcome: TerminalEventOutcome,
        action: DrainAction,
    ) -> DrainTerminalDecision:
        return await self._coordinator.handle_terminal_event(event, outcome, action)

    def next_timeout(self) -> float | None:
        return self._coordinator.next_timeout()

    async def handle_timeout(self) -> DrainLoopDecision:
        return await self._coordinator.handle_timeout()

    async def after_event(self) -> DrainLoopDecision:
        return await self._coordinator.after_event()

    def wants_aux_wake(self) -> bool:
        return self._coordinator.wants_aux_wake()

    async def wait_for_aux_wake(self) -> None:
        await self._coordinator.wait_for_aux_wake()

    async def handle_aux_wake(self) -> DrainLoopDecision:
        return await self._coordinator.handle_aux_wake()

    def handle_close(self, *, intentional_stop: bool) -> TerminalEventOutcome | None:
        return self._coordinator.handle_close(intentional_stop=intentional_stop)

    async def handle_stream_exit(
        self,
        recorded_outcome: TerminalEventOutcome | None,
    ) -> DrainExitDecision:
        return await self._coordinator.handle_stream_exit(recorded_outcome)

    def _set_awaiting_done(self, awaiting_done: bool) -> None:
        """Compatibility seam retained for existing resident test construction."""

        self._profile.set_awaiting_done(awaiting_done)


def _outstanding_descendant_blockers(
    runtime_root: Path,
    spawn_id: SpawnId,
) -> tuple[DiagnosticBlocker, ...]:
    """Compatibility seam for resident failure characterization."""

    assessment = ReconciledDescendantEvidence(
        runtime_root=runtime_root,
        root_spawn_id=spawn_id,
    ).assess()
    if assessment.failure is not None:
        raise OSError(assessment.failure.detail)
    return assessment.blockers


__all__ = ["ResidentDrainCoordinator"]
