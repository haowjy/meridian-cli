"""Shared completion state machine behind the streaming drain protocol."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from meridian.lib.streaming.completion_contracts import (
    AssessmentTrigger,
    CleanupReport,
    CompletionCleanup,
    CompletionDirectives,
    CompletionEvaluation,
    CompletionEvidence,
    CompletionPhase,
    CompletionProfile,
    CompletionState,
    EvidenceActivity,
    EvidenceEventDecision,
    EvidenceFailure,
    ProfileDecision,
    WorkAssessment,
)
from meridian.lib.streaming.drain_coordinator import (
    DrainExitDecision,
    DrainLoopDecision,
    DrainTerminalDecision,
)

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessEvent
    from meridian.lib.harness.semantics import TerminalEventOutcome
    from meridian.lib.streaming.drain_policy import DrainAction

logger = logging.getLogger(__name__)
_TIMEOUT_FLOOR_SECONDS = 0.001
_INITIAL_ASSESSMENT = WorkAssessment(disposition="ready", blockers=(), generation=0)


class CompletionCoordinator:
    """Coordinate completion mechanics while delegating evidence and precedence."""

    def __init__(
        self,
        *,
        evidence: CompletionEvidence,
        profile: CompletionProfile,
        cleanup: CompletionCleanup,
        clock: Callable[[], float] = time.monotonic,
        evaluate_without_candidate: bool = False,
    ) -> None:
        self._evidence = evidence
        self._profile = profile
        self._cleanup = cleanup
        self._clock = clock
        self._evaluate_without_candidate = evaluate_without_candidate
        self._phase: CompletionPhase = "running"
        self._candidate: TerminalEventOutcome | None = None
        self._assessment: WorkAssessment | None = None
        self._deadline_at: float | None = None
        self._deadline_latched = False
        self._stabilization_at: float | None = None
        self._stabilization_generation: int | None = None
        self._active_turn = False
        self._cleanup_attempted = False
        self._cleanup_report: CleanupReport | None = None
        self._terminal_published = False
        self._pending_evidence_activity: EvidenceActivity | None = None
        self._pending_evidence_failure: EvidenceFailure | None = None

    @property
    def state(self) -> CompletionState:
        return CompletionState(
            phase=self._phase,
            candidate=self._candidate,
            assessment=self._assessment,
            deadline_at=self._deadline_at,
            stabilization_at=self._stabilization_at,
            stabilization_generation=self._stabilization_generation,
        )

    @property
    def deadline_monotonic(self) -> float | None:
        return self._deadline_at

    @deadline_monotonic.setter
    def deadline_monotonic(self, value: float | None) -> None:
        self._deadline_at = value
        if value is not None and self._phase == "running":
            self._phase = "waiting"

    @property
    def pending_outcome(self) -> TerminalEventOutcome | None:
        return self._candidate

    @pending_outcome.setter
    def pending_outcome(self, value: TerminalEventOutcome | None) -> None:
        self._candidate = value

    @property
    def cleanup_report(self) -> CleanupReport | None:
        return self._cleanup_report

    def note_activity_transition(self, transition: str | None) -> None:
        if transition == "turn_active":
            self._active_turn = True

    async def start(self) -> None:
        await self._evidence.start()

    async def stop(self) -> None:
        await self._evidence.stop()
        self._reset_cycle()

    def next_timeout(self) -> float | None:
        if (
            self._terminal_published
            or (
                self._candidate is None
                and not self._evaluate_without_candidate
            )
        ):
            return None
        assessment = self._assessment or _INITIAL_ASSESSMENT
        candidates = [
            due
            for due in (
                self._deadline_at,
                self._stabilization_at,
                self._evidence.next_due_at(),
                self._profile.next_nudge_at(self.state, assessment),
            )
            if due is not None
        ]
        if not candidates:
            return None
        return max(min(candidates) - self._clock(), _TIMEOUT_FLOOR_SECONDS)

    async def observe_event(self, event: HarnessEvent, transition: str | None) -> bool:
        self.note_activity_transition(transition)
        decision = await self._evidence.observe_event(event, transition)
        if not decision.duplicate_canonical_event and self._evaluate_without_candidate:
            self._refresh_profile_deadline()
        return decision.duplicate_canonical_event

    def note_event_persisted(self, event: HarnessEvent) -> DrainLoopDecision:
        decision = self._evidence.note_event_persisted(event)
        self._latch_evidence_decision(decision)
        if decision.activity is not None and self._phase == "stabilizing":
            self._phase = "waiting"
            self._stabilization_at = None
            self._stabilization_generation = None
        if decision.failure is not None:
            return self._evaluate_post_persist_failure()
        return DrainLoopDecision()

    async def handle_terminal_event(
        self,
        event: HarnessEvent,
        outcome: TerminalEventOutcome,
        action: DrainAction,
    ) -> DrainTerminalDecision:
        del event
        if self._terminal_published:
            return DrainTerminalDecision()
        if outcome.status == "succeeded":
            self._candidate = outcome
            self._active_turn = False
            if action.terminate:
                self._phase = "assessing"
                return await self._evaluate_terminal(outcome, action)
        return await self._evaluate_terminal(outcome, action, assess=False)

    async def handle_timeout(self) -> DrainLoopDecision:
        return await self._evaluate_wait("timeout")

    async def after_event(self) -> DrainLoopDecision:
        return await self._evaluate_wait("event")

    def wants_aux_wake(self) -> bool:
        return (
            self._candidate is not None or self._evaluate_without_candidate
        ) and self._evidence.wants_aux_wake()

    async def wait_for_aux_wake(self) -> None:
        await self._evidence.wait_for_change()

    async def handle_aux_wake(self) -> DrainLoopDecision:
        return await self._evaluate_wait("aux_wake")

    def handle_close(self, *, intentional_stop: bool) -> TerminalEventOutcome | None:
        if self._terminal_published:
            return None
        return self._profile.close_outcome(self.state, intentional_stop)

    async def handle_stream_exit(
        self,
        recorded_outcome: TerminalEventOutcome | None,
    ) -> DrainExitDecision:
        decision = self._profile.stream_exit_decision(self.state, recorded_outcome)
        if decision.cleanup_reason is not None and not self._cleanup_attempted:
            self._cleanup_attempted = True
            assessment = self._assessment or _INITIAL_ASSESSMENT
            self._cleanup_report = await self._cleanup.cleanup(
                assessment,
                decision.cleanup_reason,
            )
        return DrainExitDecision(
            recorded_outcome=decision.recorded_outcome,
            fallback_error=decision.fallback_error,
        )

    async def _evaluate_terminal(
        self,
        outcome: TerminalEventOutcome,
        action: DrainAction,
        *,
        assess: bool = True,
    ) -> DrainTerminalDecision:
        assessment = await self._fresh_assessment("terminal_candidate") if assess else None
        now = self._clock()
        evidence_activity = self._pending_evidence_activity
        evidence_failure = self._pending_evidence_failure
        decision = self._profile.evaluate(
            CompletionEvaluation(
                state=self.state,
                trigger="terminal_candidate",
                now=now,
                directives=(
                    self._profile.consume_directives(self.state, "terminal_candidate")
                    if assess
                    else CompletionDirectives()
                ),
                assessment=assessment or self._assessment or _INITIAL_ASSESSMENT,
                deadline_expired=self._deadline_latched,
                stabilization_elapsed=self._stabilization_elapsed(now),
                profile_timer_due=self._profile_timer_due(now),
                active_turn=self._active_turn,
                candidate=self._candidate,
                terminal_outcome=outcome,
                terminal_action=action,
                evidence_activity=evidence_activity,
                evidence_failure=evidence_failure,
            )
        )
        self._clear_pending_evidence_decision()
        loop_decision = await self._apply_decision(decision, fresh_assessment=assess)
        return DrainTerminalDecision(
            recorded_outcome=loop_decision.recorded_outcome,
            emit_turn_boundary=decision.emit_turn_boundary,
        )

    async def _evaluate_wait(self, trigger: AssessmentTrigger) -> DrainLoopDecision:
        if self._terminal_published or (
            self._candidate is None and not self._evaluate_without_candidate
        ):
            return DrainLoopDecision()
        now = self._clock()
        evidence_due = self._is_due(self._evidence.next_due_at(), now)
        if evidence_due:
            self._latch_evidence_decision(await self._evidence.handle_due())
            trigger = "evidence_due"
        assessment = await self._fresh_assessment(trigger)
        now = self._clock()
        if self._is_due(self._deadline_at, now):
            self._deadline_latched = True
        decision = self._profile.evaluate(
            CompletionEvaluation(
                state=self.state,
                trigger=trigger,
                now=now,
                directives=self._profile.consume_directives(self.state, trigger),
                assessment=assessment,
                deadline_expired=self._deadline_latched,
                stabilization_elapsed=self._stabilization_elapsed(now),
                profile_timer_due=self._profile_timer_due(now),
                active_turn=self._active_turn,
                candidate=self._candidate,
                evidence_activity=self._pending_evidence_activity,
                evidence_failure=self._pending_evidence_failure,
            )
        )
        self._clear_pending_evidence_decision()
        return await self._apply_decision(decision, fresh_assessment=True)

    def _evaluate_post_persist_failure(self) -> DrainLoopDecision:
        now = self._clock()
        decision = self._profile.evaluate(
            CompletionEvaluation(
                state=self.state,
                trigger="event",
                now=now,
                directives=CompletionDirectives(),
                assessment=self._assessment or _INITIAL_ASSESSMENT,
                deadline_expired=self._deadline_latched,
                stabilization_elapsed=self._stabilization_elapsed(now),
                profile_timer_due=self._profile_timer_due(now),
                active_turn=self._active_turn,
                candidate=self._candidate,
                evidence_activity=self._pending_evidence_activity,
                evidence_failure=self._pending_evidence_failure,
            )
        )
        self._clear_pending_evidence_decision()
        if decision.action != "fail" or decision.outcome is None:
            raise RuntimeError("post-persist evidence failure must map to terminal failure")
        return self._publish(decision.outcome)

    def _latch_evidence_decision(self, decision: EvidenceEventDecision) -> None:
        if decision.activity is not None:
            self._pending_evidence_activity = decision.activity
        if decision.failure is not None:
            self._pending_evidence_failure = decision.failure

    def _clear_pending_evidence_decision(self) -> None:
        self._pending_evidence_activity = None
        self._pending_evidence_failure = None

    async def _fresh_assessment(self, trigger: AssessmentTrigger) -> WorkAssessment:
        prior_phase = self._phase
        self._phase = "assessing"
        assessment = await self._evidence.assess(trigger)
        self._assessment = assessment
        self._phase = prior_phase
        return assessment

    async def _apply_decision(
        self,
        decision: ProfileDecision,
        *,
        fresh_assessment: bool,
    ) -> DrainLoopDecision:
        now = self._clock()
        if decision.action == "clear":
            self._reset_cycle()
            return DrainLoopDecision()
        if decision.action in {"complete", "fail"}:
            outcome = decision.outcome
            assert outcome is not None
            if outcome.status == "succeeded" and not fresh_assessment:
                raise RuntimeError("successful completion requires a fresh evidence assessment")
            return self._publish(outcome)
        if decision.action == "cleanup":
            outcome = decision.outcome
            assert outcome is not None
            await self._run_cleanup_once(decision.cleanup_reason or "completion_deadline")
            return self._publish(outcome)
        if decision.action == "hold_stabilization":
            if (
                self._phase != "stabilizing"
                or self._candidate is None
                or self._stabilization_at is None
            ):
                raise RuntimeError(
                    "holding stabilization requires an active candidate and deadline"
                )
            return DrainLoopDecision()
        if decision.action == "abandon_candidate":
            self._candidate = None
        if decision.action == "stabilize":
            if decision.candidate is not None:
                self._candidate = decision.candidate
            assessment = self._assessment
            if not fresh_assessment or assessment is None or assessment.disposition != "ready":
                raise RuntimeError("stabilization requires a fresh ready assessment")
            continuing = (
                not decision.restart_stabilization
                and self._phase == "stabilizing"
                and self._stabilization_generation == assessment.generation
                and self._stabilization_at is not None
            )
            if not continuing:
                self._stabilization_generation = assessment.generation
                self._stabilization_at = now + self._profile.stabilization_seconds()
            self._phase = "stabilizing"
        else:
            self._phase = "waiting"
            self._stabilization_at = None
            self._stabilization_generation = None
        if decision.reset_deadline:
            self._deadline_latched = False
            self._deadline_at = self._profile.deadline_for(decision, now)
        elif self._deadline_at is None:
            self._deadline_at = self._profile.deadline_for(decision, now)
        if decision.nudge is not None:
            try:
                await self._profile.send_nudge(decision.nudge)
            except Exception:
                logger.exception("Completion nudge failed.")
        return DrainLoopDecision()

    async def _run_cleanup_once(self, reason: str) -> None:
        # SpawnDrainLoop serializes drain callbacks; cleanup cannot overlap here.
        if self._cleanup_attempted:
            return
        self._cleanup_attempted = True
        self._phase = "cleaning"
        self._deadline_latched = True
        self._deadline_at = None
        self._stabilization_at = None
        self._stabilization_generation = None
        assessment = self._assessment or WorkAssessment(
            disposition="unknown",
            blockers=(),
            generation=0,
            failure=EvidenceFailure(code="missing_cleanup_assessment"),
        )
        try:
            self._cleanup_report = await self._cleanup.cleanup(assessment, reason)
        except Exception as exc:
            logger.exception("Completion cleanup failed.")
            self._cleanup_report = CleanupReport(
                attempted_categories=(reason,),
                failures=(EvidenceFailure(code="cleanup_exception", detail=str(exc)),),
            )

    def _publish(self, outcome: TerminalEventOutcome) -> DrainLoopDecision:
        if self._terminal_published:
            return DrainLoopDecision()
        self._terminal_published = True
        self._phase = "finalized"
        self._candidate = None
        self._deadline_at = None
        self._stabilization_at = None
        self._stabilization_generation = None
        return DrainLoopDecision(recorded_outcome=outcome)

    def _reset_cycle(self) -> None:
        self._phase = "running"
        self._candidate = None
        self._assessment = None
        self._deadline_at = None
        self._deadline_latched = False
        self._stabilization_at = None
        self._stabilization_generation = None
        self._active_turn = False
        self._cleanup_attempted = False
        self._cleanup_report = None
        self._terminal_published = False
        self._clear_pending_evidence_decision()

    def _profile_timer_due(self, now: float) -> bool:
        assessment = self._assessment or _INITIAL_ASSESSMENT
        return self._is_due(self._profile.next_nudge_at(self.state, assessment), now)

    def _stabilization_elapsed(self, now: float) -> bool:
        return self._is_due(self._stabilization_at, now)

    def _refresh_profile_deadline(self) -> None:
        self._deadline_at = self._profile.deadline_for(
            ProfileDecision(action="wait"),
            self._clock(),
        )
        if self._deadline_at is not None and self._phase == "running":
            self._phase = "waiting"

    @staticmethod
    def _is_due(due_at: float | None, now: float) -> bool:
        return due_at is not None and now >= due_at


__all__ = ["CompletionCoordinator"]
