"""Completion policy for event-driven native background-task evidence."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.semantics import TerminalEventOutcome
from meridian.lib.streaming.completion_contracts import (
    AssessmentTrigger,
    CompletionDirectives,
    CompletionEvaluation,
    CompletionState,
    DiagnosticBlocker,
    NudgeUrgency,
    ProfileDecision,
    ProfileExitDecision,
    WorkAssessment,
)
from meridian.lib.streaming.rearm_window import RearmWindow


class NativeTaskHoldProfile:
    """Hold results until tasks and their follow-up result have drained.

    Evidence owns native snapshots, including malformed-snapshot holds and the
    empty-snapshot follow-up obligation. No snapshots means ordinary first-result
    completion. Only explicit rearm renews the absolute window; it does not prevent
    natural completion. Done authorizes leftover tasks to be stopped by managed
    teardown, but waits for an active turn's closing report until the deadline.
    Without done, expiry times out even if a closing result arrives late.

    Nudges name running tasks or the pending follow-up result; only urgent nudges
    offer rearm. Directive polling reads exact signal files, not native task trees.
    """

    def __init__(
        self, *, runtime_root: Path, spawn_id: SpawnId, deadline_seconds: float,
        poll_seconds: float, rearm_budget: int | None, clock: Callable[[], float],
        send_nudge: Callable[[str], Awaitable[None]],
    ) -> None:
        self._window = RearmWindow(
            runtime_root=runtime_root, spawn_id=spawn_id,
            deadline_seconds=deadline_seconds, rearm_budget=rearm_budget,
        )
        self._clock = clock
        self._send_nudge = send_nudge
        self._poll_seconds = poll_seconds
        self._next_directive_poll_at = clock() + poll_seconds
        self._awaiting_done = False
        self._next_nudge_at: float | None = None
        self._blockers: tuple[DiagnosticBlocker, ...] = ()

    def allows_evaluation_without_candidate(self) -> bool:
        return False

    def consume_directives(
        self, state: CompletionState, trigger: AssessmentTrigger,
    ) -> CompletionDirectives:
        del state, trigger
        self._next_directive_poll_at = self._clock() + self._poll_seconds
        return self._window.consume_directives()

    def evaluate(self, context: CompletionEvaluation) -> ProfileDecision:
        self._blockers = context.assessment.blockers
        # A terminal frame can arrive after expiry before the next timer poll.
        context = replace(context, deadline_expired=(
            context.deadline_expired
            or (context.state.deadline_at is not None and context.now >= context.state.deadline_at)
        ))
        if context.terminal_action is not None:
            return self._evaluate_terminal(context)
        return self._evaluate_wait(context)

    def deadline_for(self, decision: ProfileDecision, now: float) -> float | None:
        # Success validation is also bounded if activity interrupts the candidate.
        if decision.action not in {"wait", "stabilize", "complete"}:
            return None
        return self._window.deadline_for(now)

    def stabilization_seconds(self) -> float:
        return 0.0

    def close_outcome(
        self, state: CompletionState, intentional_stop: bool,
    ) -> TerminalEventOutcome | None:
        if state.candidate is None:
            return None
        if intentional_stop and (
            state.assessment is None or state.assessment.disposition == "ready"
        ):
            return state.candidate
        return TerminalEventOutcome(
            status=SpawnStatus.FAILED, exit_code=1, error="stream_closed_while_awaiting_done",
        )

    def next_nudge_at(self, state: CompletionState, assessment: WorkAssessment) -> float | None:
        del state
        if self._awaiting_done and assessment.disposition == "blocked":
            return self._next_nudge_at
        return None

    def next_directive_poll_at(self) -> float:
        return self._next_directive_poll_at

    async def send_nudge(self, urgency: NudgeUrgency) -> None:
        self._next_nudge_at = self._clock() + self._window.nudge_interval
        tasks = "; ".join(
            blocker.identity for blocker in self._blockers
            if blocker.code == "running" and blocker.identity is not None
        )
        if tasks:
            message = (
                f"Your turn ended while background tasks are still running: {tasks}. "
                "To keep waiting, just end your turn; you'll be resumed when they finish. "
                "If you don't need them, run `meridian spawn done` (this stops them)."
            )
        else:
            message = (
                "Your background tasks have finished; their follow-up result is still pending. "
                "Finish your report, or run `meridian spawn done` to complete now."
            )
        if urgency == "timeout_soon":
            message += " This spawn times out soon; run `meridian spawn rearm` to extend it."
        await self._send_nudge(message)

    def stream_exit_decision(
        self, state: CompletionState, recorded_outcome: TerminalEventOutcome | None,
    ) -> ProfileExitDecision:
        del state
        return ProfileExitDecision(recorded_outcome=recorded_outcome)

    def observe_activity_transition(self, transition: str | None) -> None:
        if transition == "turn_active":
            self._awaiting_done = False
            self._next_nudge_at = self._clock() + self._window.nudge_interval

    def clear(self) -> None:
        self._awaiting_done = False
        self._window.clear()
        self._next_nudge_at = None

    def after_terminal_accepted(self, outcome: TerminalEventOutcome) -> None:
        del outcome
        self.clear()

    def _propose_success(self, outcome: TerminalEventOutcome, now: float) -> ProfileDecision:
        self._enter_wait(now)
        return ProfileDecision(action="complete", outcome=outcome)

    def _evaluate_terminal(self, context: CompletionEvaluation) -> ProfileDecision:
        action, outcome = context.terminal_action, context.terminal_outcome
        assert action is not None and outcome is not None
        if not action.terminate:
            self.clear()
            return ProfileDecision(action="clear", emit_turn_boundary=action.emit_turn_boundary)
        if outcome.status != "succeeded":
            self.clear()
            return ProfileDecision(action="fail", outcome=outcome)
        if context.deadline_expired and not context.directives.rearm:
            return self._evaluate_wait(context)
        if context.directives.rearm:
            self._next_nudge_at = context.now + self._window.nudge_interval
            if not context.directives.done:
                self._enter_wait(context.now)
                return ProfileDecision(action="wait", emit_turn_boundary=True, reset_deadline=True)
        if context.directives.done or context.assessment.disposition == "ready":
            return self._propose_success(outcome, context.now)
        self._enter_wait(context.now)
        return ProfileDecision(action="wait", emit_turn_boundary=True)

    def _evaluate_wait(self, context: CompletionEvaluation) -> ProfileDecision:
        candidate = context.candidate
        assert candidate is not None
        rearmed = context.directives.rearm
        if rearmed:
            self._next_nudge_at = context.now + self._window.nudge_interval
        if context.directives.done:
            if context.active_turn and (not context.deadline_expired or rearmed):
                return ProfileDecision(action="wait", reset_deadline=rearmed)
            return self._propose_success(candidate, context.now)
        if rearmed:
            return ProfileDecision(action="wait", reset_deadline=True)
        if context.deadline_expired:
            self.clear()
            return self._window.timeout_decision()
        if context.active_turn:
            return ProfileDecision(action="wait")
        if context.assessment.disposition == "blocked":
            nudge = self._window.nudge_urgency(context) if context.profile_timer_due else None
            return ProfileDecision(action="wait", nudge=nudge)
        return self._propose_success(candidate, context.now)

    def _enter_wait(self, now: float) -> None:
        if not self._awaiting_done:
            self._next_nudge_at = now + self._window.nudge_interval
        self._awaiting_done = True
