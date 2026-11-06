"""Hold evidence, resident policy, cleanup, and the hold drain adapter."""

from __future__ import annotations

import time
from contextlib import AbstractContextManager
from dataclasses import replace
from typing import TYPE_CHECKING

import structlog

from meridian.lib.core.delivery import DeliveryRefused
from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.liveness import LivenessDecision
from meridian.lib.harness.semantics import (
    EventSemantics,
    TerminalEventOutcome,
    TerminalOutcomeCause,
)
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.spawn_aggregate import published_artifact_lock
from meridian.lib.state.spawn_signals import SpawnSignalKind, consume_resident_signals
from meridian.lib.streaming.completion_contracts import (
    COMPLETION_NUDGE_INTERVAL_SECONDS,
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

# How long a child gets to work after an idle-expiry nudge.
_IDLE_NUDGE_ANSWER_SECONDS = 300.0
# Sends one idle stretch may use: the nudge and one retry if it is refused.
_IDLE_NUDGE_SENDS = 2

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
        descendants: DescendantRefreshOwner,
        native: NativeTaskSource | None,
    ) -> None:
        self._descendants = descendants
        self._native = native

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
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
        return self._descendants.next_due_at

    async def handle_due(self) -> EvidenceEventDecision:
        self._descendants.ensure_due()
        return EvidenceEventDecision()

    def wants_aux_wake(self) -> bool:
        return self._descendants.wants_wake

    async def wait_for_change(self) -> None:
        await self._descendants.wait()

    def request_validation(self) -> int:
        return self._descendants.request()

    def validation_complete(self, request: int) -> bool:
        return self._descendants.validated(request)


class _HoldProfile:
    """Own the shared descendant/native completion handshake and deadline."""

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
        self._confirmation_requested = False
        self._confirmation_turn_started = False
        self._confirmation_failed = False
        self._done_requested = False
        self._rearmed = False
        # Blockers at the latest rearm: while they hold, turn ends are not nudged.
        self._rearm_blockers: frozenset[DiagnosticBlocker] | None = None
        # Expiry nudge sends this idle stretch; delivery spends them all. While any
        # are used the answer window runs, until the child works.
        self._idle_nudge_sends = 0
        self._last_nudge_at: float | None = None
        # After a rearm with no visible blockers (OpenCode shells, Codex tasks), the
        # next nudge waits this long past the previous one so a rearm loop stays slow.
        self._nudge_not_before: float | None = None
        self._confirmation_grace_at: float | None = None
        self._blockers: tuple[DiagnosticBlocker, ...] = ()

    def allows_evaluation_without_candidate(self) -> bool:
        return self._rearmed

    def refresh_deadline_after_event(
        self, state: CompletionState, semantics: EventSemantics
    ) -> bool:
        # Only the child's own work inside a turn renews the idle limit and answers
        # an expiry nudge. A nudge starts a turn but is not work, and output from a
        # background process after the turn ended is not the child working.
        if not (semantics.work and state.active_turn):
            return False
        self._idle_nudge_sends = 0
        return state.deadline_at is not None

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
        if state.candidate is None and not self._confirmation_requested:
            self._done_requested = False
            return CompletionDirectives()
        if self._window.grant_rearm(signals.rearm):
            self._done_requested = False
            return CompletionDirectives(rearm=True)
        self._done_requested = self._done_requested or signals.done
        return CompletionDirectives(done=self._done_requested)

    def evaluate(self, context: CompletionEvaluation) -> ProfileDecision:
        self._blockers = context.assessment.blockers
        if context.terminal_action is not None:
            return self._evaluate_terminal(context)
        return self._evaluate_wait(context)

    def deadline_for(self, decision: ProfileDecision, now: float) -> float | None:
        if decision.action not in {"wait", "stabilize", "complete", "abandon_candidate"}:
            return None
        if self._idle_nudge_outstanding:
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
        if self._rearmed:
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
        due = [self._confirmation_grace_at]
        if state.candidate is not None and self._nudge_not_before is not None:
            due.append(self._nudge_not_before if self._nudge_not_before > self._clock() else None)
        return min((at for at in due if at is not None), default=None)

    async def send_nudge(self, state: CompletionState) -> None:
        expiry_nudge = self._idle_nudge_outstanding
        confirmation_requested = self._confirmation_requested
        self._confirmation_requested = True
        self._confirmation_failed = False
        self._last_nudge_at = self._clock()
        self._confirmation_grace_at = self._last_nudge_at + self._confirmation_grace_seconds
        if self._resident_health_status() == LivenessDecision.BACKEND_DEAD:
            # An outstanding expiry nudge on a dead backend is cleaned up by the next evaluation.
            self._confirmation_failed = True
            raise RuntimeError("completion handshake backend is unavailable")
        try:
            await self._send_nudge(self._nudge_message(report_kept=state.candidate is not None))
        except Exception as exc:
            self._confirmation_failed = True
            if expiry_nudge:
                if isinstance(exc, DeliveryRefused):
                    # The child was not asked, so the next expiry retries, once.
                    self._confirmation_requested = confirmation_requested
                    self._confirmation_grace_at = None
                else:
                    # Gone, or the nudge may have arrived: never resend it.
                    self._idle_nudge_sends = _IDLE_NUDGE_SENDS
            raise
        if expiry_nudge:
            self._idle_nudge_sends = _IDLE_NUDGE_SENDS
            self._confirmation_turn_started = False

    def stream_exit_decision(
        self,
        state: CompletionState,
        recorded_outcome: TerminalEventOutcome | None,
    ) -> ProfileExitDecision:
        del state
        return ProfileExitDecision(recorded_outcome=recorded_outcome)

    def observe_event(self, semantics: EventSemantics) -> None:
        if semantics.activity != "turn_active":
            return
        self.set_awaiting_done(False)
        if self._confirmation_requested:
            self._confirmation_turn_started = True
            self._confirmation_grace_at = None

    def clear(self) -> None:
        self.set_awaiting_done(False)
        self._done_requested = False
        self._confirmation_requested = False
        self._confirmation_turn_started = False
        self._confirmation_failed = False
        self._rearmed = False
        self._rearm_blockers = None
        self._nudge_not_before = None
        self._last_nudge_at = None
        self._idle_nudge_sends = 0
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
            ):
                if context.assessment.disposition == "ready":
                    return self._propose_success(context.candidate, context.now)
                if self._expired(context):
                    # Nothing can answer a nudge on a closed transport.
                    self.clear()
                    return self._window.timeout_decision()
                if context.assessment.disposition == "unknown" or (
                    context.assessment.disposition == "blocked"
                    and any(
                        blocker.source == "persisted_descendant"
                        for blocker in context.assessment.blockers
                    )
                ):
                    if self._confirmation_requested:
                        self._confirmation_failed = True
                    self._confirmation_grace_at = None
                    self._enter_wait()
                    return ProfileDecision(action="wait", emit_turn_boundary=True)
            self.clear()
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
            return self._propose_success(context.candidate or outcome, context.now)
        if self._expired(context):
            return self._on_expiry(emit_turn_boundary=True)
        # A successful event after rearm is the next completion candidate.
        self._rearmed = False
        if context.assessment.disposition == "unknown":
            self._enter_wait()
            return ProfileDecision(action="wait", emit_turn_boundary=True)
        if not self._accepted_signals or self._confirmation_requested:
            if context.assessment.disposition == "ready":
                return self._propose_success(context.candidate or outcome, context.now)
            self._enter_wait()
            return ProfileDecision(action="wait", emit_turn_boundary=True)
        self._enter_wait()
        return ProfileDecision(
            action="wait",
            emit_turn_boundary=True,
            nudge=not self._nudge_held(context.now),
        )

    def _evaluate_wait(self, context: CompletionEvaluation) -> ProfileDecision:
        if self._idle_nudge_outstanding and self._resident_health_status() == (
            LivenessDecision.BACKEND_DEAD
        ):
            # The expiry nudge went to a dead backend: nothing can answer it.
            self.clear()
            return self._window.timeout_decision()
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
            if self._expired(context):
                return self._on_expiry()
            self._enter_wait()
            return ProfileDecision(action="wait")
        if context.directives.done:
            if context.assessment.disposition == "unknown":
                if self._expired(context):
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
            if context.state.active_turn:
                # Done waits out a running turn; a turn idle for a whole window is
                # asked once, not ended as a success it never finished.
                if self._expired(context):
                    return self._on_expiry()
                return ProfileDecision(action="wait")
            return self._propose_success(candidate, context.now)
        if self._expired(context):
            return self._on_expiry()
        if context.state.active_turn:
            return ProfileDecision(action="wait")
        if context.assessment.disposition == "unknown":
            return ProfileDecision(action="wait")
        if (
            self._accepted_signals
            and not self._confirmation_requested
            and not self._rearmed
            and not self._idle_nudge_outstanding
            and not self._nudge_held(context.now)
        ):
            self._enter_wait()
            return ProfileDecision(action="wait", nudge=True)
        if context.assessment.disposition == "ready" and self._confirmation_settled:
            return self._propose_success(candidate, context.now)
        self._enter_wait()
        return ProfileDecision(action="wait")

    @staticmethod
    def _expired(context: CompletionEvaluation) -> bool:
        # Read the live deadline: the coordinator's latch outlives an event-driven renewal.
        deadline_at = context.state.deadline_at
        return deadline_at is not None and context.now >= deadline_at

    def _on_expiry(self, *, emit_turn_boundary: bool = False) -> ProfileDecision:
        """Nudge once, then clean up if the child does not work in the answer window."""
        if (
            self._accepted_signals
            and self._idle_nudge_sends < _IDLE_NUDGE_SENDS
            and self._resident_health_status() != LivenessDecision.BACKEND_DEAD
        ):
            self._idle_nudge_sends += 1
            self._enter_wait()
            return ProfileDecision(
                action="wait",
                emit_turn_boundary=emit_turn_boundary,
                nudge=True,
                reset_deadline=True,
            )
        self.clear()
        return self._window.timeout_decision()

    def _nudge_held(self, now: float) -> bool:
        """True while a rearm still answers the nudge: blockers unchanged, or too soon."""
        if self._rearm_blockers_hold():
            return True
        return self._nudge_not_before is not None and now < self._nudge_not_before

    def _rearm_blockers_hold(self) -> bool:
        """True while the blockers a rearm answered are all that is outstanding.

        The follow-up result Claude sends on its own does not count as a change.
        """
        held = self._rearm_blockers
        if held is None:
            return False
        if self._blockers and all(
            blocker in held or blocker.code == "awaiting_followup_result"
            for blocker in self._blockers
        ):
            return True
        self._rearm_blockers = None
        return False

    def _mark_rearmed(self) -> None:
        self._rearm_blockers = frozenset(self._blockers) or None
        self._nudge_not_before = (
            None
            if self._rearm_blockers is not None or self._last_nudge_at is None
            else self._last_nudge_at + COMPLETION_NUDGE_INTERVAL_SECONDS
        )
        self._idle_nudge_sends = 0
        self._confirmation_requested = False
        self._confirmation_turn_started = False
        self._confirmation_failed = False
        self._rearmed = True
        self._confirmation_grace_at = None

    @property
    def _idle_nudge_outstanding(self) -> bool:
        return self._idle_nudge_sends > 0

    @property
    def _confirmation_settled(self) -> bool:
        return (
            not self._accepted_signals
            or self._confirmation_failed
            or self._confirmation_turn_started
        )

    @property
    def accepted_signals(self) -> frozenset[SpawnSignalKind]:
        return self._accepted_signals

    def _enter_wait(self) -> None:
        self.set_awaiting_done(True)

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
            # Codex and OpenCode take this nudge into the running turn, so the child's
            # answer becomes the report: a "Done." here would replace it.
            message += (
                "No report is kept for this spawn yet. If you are finished, run "
                "`meridian spawn done`, then make your last message the complete report. "
            )
        message += (
            "If more work remains, including waiting on background tasks or spawns, run "
            "`meridian spawn rearm` first, then finish with a complete report."
        )
        return message

    def set_awaiting_done(self, awaiting_done: bool) -> None:
        if awaiting_done == self._awaiting_done:
            return
        if self._backend is not None:
            self._backend.set_awaiting_done(awaiting_done)
        self._awaiting_done = awaiting_done

    def _resident_health_status(self) -> LivenessDecision:
        if self._backend is None:
            # No backend liveness policy: the connection's own health is the evidence.
            # A send that found the transport gone (TransportDead) leaves it unhealthy,
            # so the evaluation that follows takes the dead-backend cleanup.
            return LivenessDecision.CONTINUE if self._health() else LivenessDecision.BACKEND_DEAD
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
        launch: LaunchPaths,
        send_nudge: Callable[[str], Awaitable[None]],
        completion_signals_available: bool,
        backend: ResidentBackendControl | None,
        health: Callable[[], bool],
        native: NativeTaskSource | None,
        deadline_seconds: float,
        poll_seconds: float | None,
        rearm_budget: int | None,
        cancel_descendants: Callable[[SpawnId], Awaitable[set[str]]],
    ) -> ResidentDrainCoordinator:
        if deadline_seconds <= 0:
            raise ValueError("resident completion deadline must be positive")
        runtime_root = launch.runtime_root
        spawn_id = launch.spawn_id
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
            launch=launch,
            backend=backend,
            health=health,
            send_nudge=send_nudge,
            completion_signals_available=completion_signals_available,
            deadline_seconds=deadline_seconds,
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

    @property
    def accepted_signals(self) -> frozenset[SpawnSignalKind]:
        return self._profile.accepted_signals

    async def start(self) -> None:
        await self._coordinator.start()

    async def stop(self) -> None:
        self._profile.clear()
        await self._coordinator.stop()

    async def observe_event(self, event: NormalizedHarnessEvent) -> NormalizedHarnessEvent | None:
        self._profile.observe_event(event.semantics)
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
