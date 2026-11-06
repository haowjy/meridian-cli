"""The prototype shell around HoldState, for the walk. Step 2 moves it into `_HoldProfile`."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import replace
from typing import TYPE_CHECKING

from meridian.lib.core.delivery import DeliveryRefused, TransportDead
from meridian.lib.core.domain import SpawnStatus
from meridian.lib.harness.connections.liveness import LivenessDecision
from meridian.lib.harness.semantics import TerminalEventOutcome
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.spawn_aggregate import published_artifact_lock
from meridian.lib.state.spawn_signals import SpawnSignalKind, consume_resident_signals
from meridian.lib.streaming.completion_contracts import (
    AssessmentTrigger,
    CompletionDirectives,
    CompletionEvaluation,
    CompletionState,
    DiagnosticBlocker,
    ProfileDecision,
    ProfileExitDecision,
    WorkAssessment,
)
from meridian.lib.streaming.hold_state import (
    AskPhase,
    HoldFacts,
    HoldState,
    SendResult,
    SpacedLease,
    TurnEnd,
    after_event,
    after_send,
    decide,
    grace_ends,
)
from meridian.lib.streaming.rearm_window import RearmWindow
from tests.hold_walk.frozen_resident_drain import (
    _BACKEND_DEAD_WHILE_AWAITING_DONE,
    _IDLE_NUDGE_ANSWER_SECONDS,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from meridian.lib.harness.connections.resident_backend import ResidentBackendControl
    from meridian.lib.harness.semantics import EventSemantics


class HoldProfile:
    """Drop-in `_HoldProfile` replacement. Same constructor, send still raises."""

    def __init__(
        self,
        *,
        launch: LaunchPaths,
        backend: ResidentBackendControl | None,
        health: Callable[[], bool],
        send_nudge: Callable[[str], Awaitable[None]],
        deadline_seconds: float,
        poll_seconds: float,
        rearm_budget: int | None,
        clock: Callable[[], float],
        completion_signals_available: bool,
    ) -> None:
        self._launch = launch
        self._runtime_root = launch.runtime_root
        self._spawn_id = launch.spawn_id
        self._window = RearmWindow(
            runtime_root=launch.runtime_root,
            spawn_id=launch.spawn_id,
            deadline_seconds=deadline_seconds,
            rearm_budget=rearm_budget,
        )
        self._backend = backend
        self._health = health
        self._send_nudge = send_nudge
        self._accepted_signals: frozenset[SpawnSignalKind] = (
            frozenset({"done", "rearm"}) if completion_signals_available else frozenset()
        )
        self._clock = clock
        self._confirmation_grace_seconds = poll_seconds
        self._awaiting_done = False
        self._state = HoldState()
        self._blockers: tuple[DiagnosticBlocker, ...] = ()
        self.last_row: str = ""

    def allows_evaluation_without_candidate(self) -> bool:
        return self._state.rearmed

    def refresh_deadline_after_event(
        self, state: CompletionState, semantics: EventSemantics
    ) -> bool:
        turn_started = semantics.activity == "turn_active"
        work_in_turn = semantics.work and state.active_turn
        if turn_started:
            self.set_awaiting_done(False)
        self._state = after_event(self._state, turn_started=turn_started, work_in_turn=work_in_turn)
        return work_in_turn and state.deadline_at is not None

    def observe_event(self, semantics: EventSemantics) -> None:
        # The frozen coordinator's order: the walk calls this first on both sides.
        if semantics.activity == "turn_active":
            self.set_awaiting_done(False)

    def candidate_transition(self) -> AbstractContextManager[None]:
        return published_artifact_lock(self._runtime_root, self._spawn_id)

    def consume_directives(
        self,
        state: CompletionState,
        trigger: AssessmentTrigger,
    ) -> CompletionDirectives:
        del trigger
        if not self._accepted_signals:
            return CompletionDirectives()
        signals = consume_resident_signals(self._launch)
        if state.candidate is None and self._state.ask == AskPhase.UNASKED:
            self._state = replace(self._state, closing=False)
            return CompletionDirectives()
        if self._window.grant_rearm(signals.rearm):
            self._state = replace(self._state, closing=False)
            return CompletionDirectives(rearm=True)
        closing = self._state.closing or signals.done
        self._state = replace(self._state, closing=closing)
        return CompletionDirectives(done=closing)

    def evaluate(self, context: CompletionEvaluation) -> ProfileDecision:
        self._blockers = context.assessment.blockers
        if context.directives.rearm:
            self._window.persist_count()
        end = None
        if context.terminal_action is not None and context.terminal_outcome is not None:
            end = TurnEnd(context.terminal_outcome, context.terminal_action)
        dead = self._resident_health_status() == LivenessDecision.BACKEND_DEAD
        step = decide(
            self._state,
            HoldFacts(
                now=context.now,
                end=end,
                active_turn=context.state.active_turn,
                expired=(
                    context.state.deadline_at is not None
                    and context.now >= context.state.deadline_at
                ),
                candidate=context.candidate,
                assessment=context.assessment,
                rearm=context.directives.rearm,
                dead=dead,
                accepts_signals=bool(self._accepted_signals),
                grace_seconds=self._confirmation_grace_seconds,
                timeout=self._window.timeout_decision(),
            ),
        )
        self.last_row = step.row
        self._state = step.state
        if step.decision.action in {"clear", "fail", "cleanup"}:
            self.set_awaiting_done(False)
        elif step.enter_wait:
            self.set_awaiting_done(True)
        return step.decision

    def deadline_for(self, decision: ProfileDecision, now: float) -> float | None:
        if decision.action not in {"wait", "stabilize", "complete", "abandon_candidate"}:
            return None
        if self._state.answer is not None:
            return now + _IDLE_NUDGE_ANSWER_SECONDS
        return self._window.deadline_for(now)

    def stabilization_seconds(self) -> float:
        return 0.0

    def close_outcome(
        self, state: CompletionState, intentional_stop: bool
    ) -> TerminalEventOutcome | None:
        if state.candidate is None:
            return None
        if state.assessment is not None and state.assessment.disposition == "ready":
            return state.candidate
        if intentional_stop:
            return TerminalEventOutcome(
                status=SpawnStatus.FAILED,
                exit_code=1,
                error="stream_closed_while_awaiting_done",
            )
        if self._resident_health_status() == LivenessDecision.BACKEND_DEAD:
            return _BACKEND_DEAD_WHILE_AWAITING_DONE
        return TerminalEventOutcome(
            status=SpawnStatus.FAILED,
            exit_code=1,
            error="stream_closed_while_awaiting_done",
        )

    def should_defer_close(self, state: CompletionState) -> bool:
        if self._resident_health_status() == LivenessDecision.BACKEND_DEAD:
            return False
        if self._state.rearmed:
            return True
        return (
            state.candidate is not None
            and state.assessment is not None
            and state.assessment.disposition == "blocked"
            and any(
                blocker.source == "persisted_descendant" for blocker in state.assessment.blockers
            )
        )

    def next_nudge_at(self, state: CompletionState, assessment: WorkAssessment) -> float | None:
        del assessment
        due: list[float] = []
        if self._state.ask is AskPhase.SENT:
            due.append(grace_ends(self._state, self._confirmation_grace_seconds))
        lease = self._state.lease
        if (
            state.candidate is not None
            and isinstance(lease, SpacedLease)
            and lease.not_before > self._clock()
        ):
            due.append(lease.not_before)
        return min(due, default=None)

    async def send_nudge(self, state: CompletionState) -> None:
        prior = self._state.ask
        # Assume sent before awaiting: a failure that escapes must not re-fire the ask.
        self._state = replace(self._state, ask=AskPhase.SENT, asked_at=self._clock())
        if self._resident_health_status() == LivenessDecision.BACKEND_DEAD:
            result = SendResult.DEAD
        else:
            try:
                await self._send_nudge(self._nudge_message(report_kept=state.candidate is not None))
                result = SendResult.SENT
            except DeliveryRefused:
                result = SendResult.REFUSED
            except TransportDead:
                result = SendResult.DEAD
            except Exception:
                result = SendResult.SENT  # may have arrived: never resend
        self._state = after_send(self._state, result, prior=prior, now=self._clock())
        if result is not SendResult.SENT:
            raise RuntimeError(f"completion ask failed: {result}")

    def stream_exit_decision(
        self,
        state: CompletionState,
        recorded_outcome: TerminalEventOutcome | None,
    ) -> ProfileExitDecision:
        del state
        return ProfileExitDecision(
            recorded_outcome=recorded_outcome,
            cleanup_reason=(
                _BACKEND_DEAD_WHILE_AWAITING_DONE.error
                if recorded_outcome is _BACKEND_DEAD_WHILE_AWAITING_DONE
                else None
            ),
        )

    def clear(self) -> None:
        self.set_awaiting_done(False)
        self._state = HoldState()
        self._blockers = ()

    def after_terminal_accepted(self, outcome: TerminalEventOutcome) -> None:
        del outcome
        self.clear()

    @property
    def accepted_signals(self) -> frozenset[SpawnSignalKind]:
        return self._accepted_signals

    def set_awaiting_done(self, awaiting_done: bool) -> None:
        if awaiting_done == self._awaiting_done:
            return
        if self._backend is not None:
            self._backend.set_awaiting_done(awaiting_done)
        self._awaiting_done = awaiting_done

    def _nudge_message(self, *, report_kept: bool) -> str:
        descendants = [
            blocker.identity
            for blocker in self._blockers
            if blocker.source == "persisted_descendant" and blocker.identity is not None
        ]
        tasks = [
            blocker.identity
            for blocker in self._blockers
            if blocker.source == "native_task"
            and blocker.code == "running"
            and blocker.identity is not None
        ]
        awaiting_result = any(
            blocker.source == "native_task" and blocker.code == "awaiting_followup_result"
            for blocker in self._blockers
        )
        details: list[str] = []
        if descendants:
            ids = " ".join(descendants)
            details.append(
                f"Descendant spawns are still running: {', '.join(descendants)}. "
                f"Run `meridian spawn wait {ids}` before finishing."
            )
        if tasks:
            details.append(f"Background tasks are still running: {'; '.join(tasks)}.")
        if awaiting_result:
            details.append("Background tasks finished, but their follow-up result is pending.")
        if details:
            message = " ".join(details) + " "
        else:
            message = "Before this spawn closes, confirm whether you are actually finished. "
        if report_kept:
            message += (
                "Your previous reply is kept as this spawn's report. If it is the final report, "
                "run `meridian spawn done` and end the turn. "
            )
        else:
            message += (
                "No report is kept for this spawn yet. If you are finished, run "
                "`meridian spawn done`, then make your last message the complete report. "
            )
        message += (
            "If more work remains, including waiting on background tasks or spawns, run "
            "`meridian spawn rearm` first, then finish with a complete report."
        )
        return message

    def _resident_health_status(self) -> LivenessDecision:
        if self._backend is None:
            return LivenessDecision.CONTINUE if self._health() else LivenessDecision.BACKEND_DEAD
        try:
            return self._backend.health_status()
        except Exception:
            return LivenessDecision.BACKEND_DEAD
