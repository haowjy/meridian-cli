"""Hold evidence, resident policy, cleanup, and the hold drain adapter."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.liveness import LivenessDecision
from meridian.lib.harness.semantics import TerminalEventOutcome, TerminalOutcomeCause
from meridian.lib.streaming.completion_contracts import (
    AssessmentTrigger,
    CleanupReport,
    CompletionCleanupRequest,
    CompletionDirectives,
    CompletionEvaluation,
    CompletionState,
    DiagnosticBlocker,
    EvidenceEventDecision,
    EvidenceFailure,
    NativeTaskSource,
    NudgeUrgency,
    ProfileDecision,
    ProfileExitDecision,
    WorkAssessment,
)
from meridian.lib.streaming.completion_coordinator import CompletionCoordinator
from meridian.lib.streaming.descendant_evidence import (
    DescendantRefreshOwner,
    ReconciledDescendantEvidence,
)
from meridian.lib.streaming.drain_coordinator import (
    DrainExitDecision,
    DrainLoopDecision,
    DrainTerminalDecision,
)
from meridian.lib.streaming.rearm_window import RearmWindow

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from meridian.lib.harness.connections.base import RawHarnessEvent
    from meridian.lib.harness.connections.resident_backend import ResidentBackendControl
    from meridian.lib.harness.semantics import NormalizedHarnessEvent
    from meridian.lib.streaming.drain_policy import DrainAction

logger = structlog.get_logger()

_BACKEND_DEAD_WHILE_AWAITING_DONE = TerminalEventOutcome(
    status=SpawnStatus.FAILED,
    exit_code=1,
    error="backend_dead_while_awaiting_done",
)


class HoldEvidence:
    """Merge persisted-descendant evidence with an optional native task source.

    Unreadable descendants dominate and carry their failure; otherwise the hold
    is blocked while either source has blockers. Native facts update with the
    stream, so success validation waits only on a fresh descendant read.
    Descendant refresh starts only after the first completion candidate.
    """

    def __init__(
        self,
        *,
        descendants: DescendantRefreshOwner | None,
        native: NativeTaskSource | None,
    ) -> None:
        self._descendants = descendants
        self._native = native

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        if self._descendants is not None:
            await self._descendants.stop()

    async def observe_event(
        self, event: RawHarnessEvent, transition: str | None
    ) -> EvidenceEventDecision:
        del transition
        if self._native is not None:
            self._native.observe_event(event)
        return EvidenceEventDecision()

    def note_event_delivered(
        self, event: RawHarnessEvent, state: CompletionState
    ) -> EvidenceEventDecision:
        del event, state
        return EvidenceEventDecision()

    async def assess(self, trigger: AssessmentTrigger, state: CompletionState) -> WorkAssessment:
        del trigger, state
        native_blockers = () if self._native is None else self._native.blockers
        generation = 0 if self._native is None else self._native.generation
        if self._descendants is None:
            descendant_blockers: tuple[DiagnosticBlocker, ...] = ()
        else:
            self._descendants.ensure_due()
            descendants = self._descendants.assessment
            generation += descendants.generation
            if descendants.disposition == "unknown":
                return replace(descendants, generation=generation)
            descendant_blockers = descendants.blockers
        blockers = descendant_blockers + native_blockers
        return WorkAssessment(
            disposition="blocked" if blockers else "ready",
            blockers=blockers,
            generation=generation,
        )

    def next_due_at(self) -> float | None:
        return None if self._descendants is None else self._descendants.next_due_at

    async def handle_due(self) -> EvidenceEventDecision:
        if self._descendants is not None:
            self._descendants.ensure_due()
        return EvidenceEventDecision()

    def wants_aux_wake(self) -> bool:
        return self._descendants is not None and self._descendants.wants_wake

    async def wait_for_change(self) -> None:
        if self._descendants is None:
            raise RuntimeError("native-only hold evidence has no auxiliary wake source")
        await self._descendants.wait()

    def request_validation(self) -> int:
        return 0 if self._descendants is None else self._descendants.request()

    def validation_complete(self, request: int) -> bool:
        return self._descendants is None or self._descendants.validated(request)


class _HoldProfile:
    """Own the shared descendant/native completion handshake and deadline."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        backend: ResidentBackendControl | None,
        send_nudge: Callable[[str], Awaitable[None]],
        deadline_seconds: float,
        poll_seconds: float,
        rearm_budget: int | None,
        clock: Callable[[], float],
    ) -> None:
        self._window = RearmWindow(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            deadline_seconds=deadline_seconds,
            rearm_budget=rearm_budget,
        )
        self._backend = backend
        self._send_nudge = send_nudge
        self._clock = clock
        self._confirmation_grace_seconds = poll_seconds
        self._awaiting_done = False
        self._confirmation_requested = False
        self._confirmation_turn_started = False
        self._confirmation_failed = False
        self._rearmed = False
        self._confirmation_grace_at: float | None = None
        self._blockers: tuple[DiagnosticBlocker, ...] = ()

    def allows_evaluation_without_candidate(self) -> bool:
        return self._rearmed

    def refresh_deadline_after_event(self) -> bool:
        return False

    def consume_directives(
        self,
        state: CompletionState,
        trigger: AssessmentTrigger,
    ) -> CompletionDirectives:
        del state, trigger
        return self._window.consume_directives()

    def evaluate(self, context: CompletionEvaluation) -> ProfileDecision:
        self._blockers = context.assessment.blockers
        if context.terminal_action is not None:
            return self._evaluate_terminal(context)
        return self._evaluate_wait(context)

    def deadline_for(self, decision: ProfileDecision, now: float) -> float | None:
        if decision.action not in {"wait", "stabilize", "complete", "abandon_candidate"}:
            return None
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
        if self._rearmed:
            return True
        return (
            state.candidate is not None
            and state.assessment is not None
            and (
                (
                    self._confirmation_failed
                    and state.assessment.disposition == "unknown"
                )
                or (
                    state.assessment.disposition == "blocked"
                    and any(
                        blocker.source == "persisted_descendant"
                        for blocker in state.assessment.blockers
                    )
                )
            )
        )

    def next_nudge_at(self, state: CompletionState, assessment: WorkAssessment) -> float | None:
        del state, assessment
        return self._confirmation_grace_at

    def next_directive_poll_at(self) -> None:
        return None

    async def send_nudge(self, urgency: NudgeUrgency) -> None:
        self._confirmation_requested = True
        self._confirmation_failed = False
        self._confirmation_grace_at = self._clock() + self._confirmation_grace_seconds
        if self._resident_health_status() == LivenessDecision.BACKEND_DEAD:
            self._confirmation_failed = True
            raise RuntimeError("completion handshake backend is unavailable")
        try:
            await self._send_nudge(self._nudge_message(urgency))
        except Exception:
            self._confirmation_failed = True
            raise

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
        if self._confirmation_requested:
            self._confirmation_turn_started = True
            self._confirmation_grace_at = None

    def clear(self) -> None:
        self.set_awaiting_done(False)
        self._window.clear()
        self._confirmation_requested = False
        self._confirmation_turn_started = False
        self._confirmation_failed = False
        self._rearmed = False
        self._confirmation_grace_at = None
        self._blockers = ()

    def after_terminal_accepted(self, outcome: TerminalEventOutcome) -> None:
        del outcome
        self.clear()

    def _propose_success(self, outcome: TerminalEventOutcome, now: float) -> ProfileDecision:
        del now
        self._enter_wait()
        return ProfileDecision(action="complete", outcome=outcome)

    def _evaluate_terminal(self, context: CompletionEvaluation) -> ProfileDecision:
        action = context.terminal_action
        outcome = context.terminal_outcome
        assert action is not None
        assert outcome is not None
        if not action.terminate:
            self.clear()
            return ProfileDecision(action="clear", emit_turn_boundary=action.emit_turn_boundary)
        if outcome.status != "succeeded":
            if (
                context.candidate is not None
                and outcome.cause is TerminalOutcomeCause.REPLACEABLE_TRANSPORT_CLOSE
                and self._resident_health_status() == LivenessDecision.BACKEND_DEAD
            ):
                if context.deadline_expired:
                    self.clear()
                    return self._window.timeout_decision()
                if context.assessment.disposition == "ready":
                    return self._propose_success(context.candidate, context.now)
                if context.assessment.disposition == "unknown" or (
                    context.assessment.disposition == "blocked"
                    and any(
                        blocker.source == "persisted_descendant"
                        for blocker in context.assessment.blockers
                    )
                ):
                    self._confirmation_failed = True
                    self._confirmation_grace_at = None
                    self._enter_wait()
                    return ProfileDecision(action="wait", emit_turn_boundary=True)
            self.clear()
            if (
                context.candidate is not None
                and outcome.cause is TerminalOutcomeCause.REPLACEABLE_TRANSPORT_CLOSE
                and self._resident_health_status() == LivenessDecision.BACKEND_DEAD
            ):
                return ProfileDecision(
                    action="fail",
                    outcome=_BACKEND_DEAD_WHILE_AWAITING_DONE,
                )
            return ProfileDecision(action="fail", outcome=outcome)
        if context.directives.rearm:
            self._mark_rearmed()
            self._enter_wait()
            return ProfileDecision(
                action="abandon_candidate",
                emit_turn_boundary=True,
                reset_deadline=True,
            )
        if context.directives.done:
            if context.assessment.disposition == "unknown":
                self._enter_wait()
                return ProfileDecision(action="wait", emit_turn_boundary=True)
            return self._propose_success(outcome, context.now)
        if context.deadline_expired:
            self.clear()
            return self._window.timeout_decision()
        # A successful event after rearm is the next completion candidate.
        self._rearmed = False
        if context.assessment.disposition == "unknown":
            self._enter_wait()
            return ProfileDecision(action="wait", emit_turn_boundary=True)
        if self._confirmation_requested:
            if context.assessment.disposition == "ready":
                return self._propose_success(outcome, context.now)
            self._enter_wait()
            return ProfileDecision(action="wait", emit_turn_boundary=True)
        self._enter_wait()
        return ProfileDecision(
            action="wait",
            emit_turn_boundary=True,
            nudge="normal",
        )

    def _evaluate_wait(self, context: CompletionEvaluation) -> ProfileDecision:
        candidate = context.candidate
        rearmed = context.directives.rearm
        if rearmed:
            self._mark_rearmed()
            self._enter_wait()
            return ProfileDecision(action="abandon_candidate", reset_deadline=True)
        confirmation_grace_expired = (
            self._confirmation_grace_at is not None
            and context.now >= self._confirmation_grace_at
            and not self._confirmation_turn_started
        )
        if confirmation_grace_expired:
            self._confirmation_grace_at = None
            self._confirmation_failed = True
        if candidate is None:
            if context.deadline_expired:
                self.clear()
                return self._window.timeout_decision()
            self._enter_wait()
            return ProfileDecision(action="wait")
        if context.directives.done:
            if context.assessment.disposition == "unknown":
                if context.deadline_expired:
                    self.clear()
                    return ProfileDecision(
                        action="fail",
                        outcome=TerminalEventOutcome(
                            status=SpawnStatus.FAILED,
                            exit_code=1,
                            error="resident_evidence_unreadable",
                        ),
                    )
                return ProfileDecision(action="wait")
            if context.active_turn and not context.deadline_expired:
                return ProfileDecision(action="wait")
            return self._propose_success(candidate, context.now)
        if context.deadline_expired:
            self.clear()
            return self._window.timeout_decision()
        if context.active_turn:
            return ProfileDecision(action="wait")
        if context.assessment.disposition == "unknown":
            return ProfileDecision(action="wait")
        if not self._confirmation_requested and not self._rearmed:
            self._enter_wait()
            return ProfileDecision(action="wait", nudge="normal")
        if context.assessment.disposition == "ready" and (
            self._confirmation_failed or self._confirmation_turn_started
        ):
            return self._propose_success(candidate, context.now)
        if confirmation_grace_expired:
            self._enter_wait()
            return ProfileDecision(action="wait")
        self._enter_wait()
        return ProfileDecision(action="wait")

    def _mark_rearmed(self) -> None:
        self._confirmation_requested = False
        self._confirmation_turn_started = False
        self._confirmation_failed = False
        self._rearmed = True
        self._confirmation_grace_at = None

    def _enter_wait(self) -> None:
        self.set_awaiting_done(True)

    def _nudge_message(self, urgency: NudgeUrgency) -> str:
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
        message += (
            "When finished, run `meridian spawn done` and then provide your complete final "
            "report. If more work remains, run `meridian spawn rearm` and continue."
        )
        if urgency == "timeout_soon":
            message += " This spawn's completion window expires soon."
        return message

    def set_awaiting_done(self, awaiting_done: bool) -> None:
        if awaiting_done == self._awaiting_done:
            return
        if self._backend is not None:
            self._backend.set_awaiting_done(awaiting_done)
        self._awaiting_done = awaiting_done

    def _resident_health_status(self) -> LivenessDecision:
        if self._backend is None:
            return LivenessDecision.CONTINUE
        try:
            return self._backend.health_status()
        except Exception:
            return LivenessDecision.BACKEND_DEAD


class _ResidentCompletionCleanup:
    """Cancel resident descendants through the canonical application service."""

    def __init__(
        self,
        *,
        spawn_id: SpawnId,
        cancel_descendants: Callable[[SpawnId], Awaitable[set[str]]],
    ) -> None:
        self._spawn_id = spawn_id
        self._cancel_descendants = cancel_descendants

    async def cleanup(self, assessment: WorkAssessment, reason: str) -> CleanupReport:
        del assessment
        try:
            cancelled = await self._cancel_descendants(self._spawn_id)
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
    """Construct and adapt hold collaborators to the drain coordinator protocol."""

    def __init__(
        self,
        *,
        coordinator: CompletionCoordinator,
        profile: _HoldProfile,
    ) -> None:
        self._coordinator = coordinator
        self._profile = profile

    @classmethod
    def build(
        cls,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        send_nudge: Callable[[str], Awaitable[None]],
        backend: ResidentBackendControl | None,
        native: NativeTaskSource | None,
        deadline_seconds: float | None,
        poll_seconds: float | None,
        rearm_budget: int | None,
        cancel_descendants: Callable[[SpawnId], Awaitable[set[str]]],
    ) -> ResidentDrainCoordinator:
        resolved_deadline = (
            deadline_seconds if deadline_seconds and deadline_seconds > 0 else 3300.0
        )
        resolved_poll = poll_seconds if poll_seconds and poll_seconds > 0 else 5.0
        clock = time.monotonic
        if native is None and backend is None:
            raise ValueError("a hold needs a native task source or resident backend control")
        evidence = HoldEvidence(
            descendants=DescendantRefreshOwner(
                ReconciledDescendantEvidence(
                    runtime_root=runtime_root,
                    root_spawn_id=spawn_id,
                ),
                poll_seconds=resolved_poll,
                clock=clock,
            ),
            native=native,
        )
        profile = _HoldProfile(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            backend=backend,
            send_nudge=send_nudge,
            deadline_seconds=resolved_deadline,
            poll_seconds=resolved_poll,
            rearm_budget=rearm_budget,
            clock=clock,
        )
        coordinator = CompletionCoordinator(
            evidence=evidence,
            profile=profile,
            cleanup=_ResidentCompletionCleanup(
                spawn_id=spawn_id,
                cancel_descendants=cancel_descendants,
            ),
            clock=clock,
        )
        return cls(coordinator=coordinator, profile=profile)

    async def start(self) -> None:
        await self._coordinator.start()

    async def stop(self) -> None:
        self._profile.clear()
        await self._coordinator.stop()

    def observe_activity_transition(self, transition: str | None) -> None:
        self._profile.observe_activity_transition(transition)
        self._coordinator.note_activity_transition(transition)

    async def observe_event(self, event: NormalizedHarnessEvent) -> NormalizedHarnessEvent | None:
        self._profile.observe_activity_transition(event.semantics.activity)
        return await self._coordinator.observe_event(event)

    def note_event_delivered(self, event: RawHarnessEvent) -> DrainLoopDecision:
        return self._coordinator.note_event_delivered(event)

    async def handle_terminal_event(
        self,
        event: RawHarnessEvent,
        outcome: TerminalEventOutcome,
        action: DrainAction,
    ) -> DrainTerminalDecision:
        return await self._coordinator.handle_terminal_event(event, outcome, action)

    def next_timeout(self) -> float | None:
        return self._coordinator.next_timeout()

    @property
    def pending_outcome(self) -> TerminalEventOutcome | None:
        return self._coordinator.pending_outcome

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

    def should_defer_close(self) -> bool:
        return self._coordinator.should_defer_close()

    async def handle_stream_exit(
        self,
        recorded_outcome: TerminalEventOutcome | None,
    ) -> DrainExitDecision:
        return await self._coordinator.handle_stream_exit(recorded_outcome)

    async def execute_post_publication_cleanup(
        self,
        request: CompletionCleanupRequest,
    ) -> None:
        await self._coordinator.execute_post_publication_cleanup(request)


__all__ = ["ResidentDrainCoordinator"]
