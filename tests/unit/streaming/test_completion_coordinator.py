"""Functional-core transitions for profile-driven completion."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

import pytest

from meridian.lib.harness.semantics import TerminalEventOutcome
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
    ProfileExitDecision,
    WorkAssessment,
)
from meridian.lib.streaming.completion_coordinator import CompletionCoordinator
from meridian.lib.streaming.drain_policy import DrainAction
from tests.support.fakes import FakeClock

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import RawHarnessEvent

_SUCCESS = TerminalEventOutcome(status="succeeded", exit_code=0)
_TIMEOUT = TerminalEventOutcome(status="timed_out", exit_code=1, error="deadline")
_FAILURE = TerminalEventOutcome(status="failed", exit_code=1, error="evidence_failure")
_TERMINATE = DrainAction(terminate=True, emit_turn_boundary=False)


def _ready(generation: int = 1) -> WorkAssessment:
    return WorkAssessment(disposition="ready", blockers=(), generation=generation)


def _blocked(generation: int = 1) -> WorkAssessment:
    return WorkAssessment(
        disposition="blocked",
        blockers=(DiagnosticBlocker(source="descendant", code="active", identity="p2"),),
        generation=generation,
    )


def _unknown(generation: int = 1) -> WorkAssessment:
    return WorkAssessment(
        disposition="unknown",
        blockers=(),
        generation=generation,
        failure=EvidenceFailure(code="read_failed"),
    )


class _Evidence:
    def __init__(self, *assessments: WorkAssessment) -> None:
        self.assessments = deque(assessments)
        self.last = assessments[-1]
        self.persisted = EvidenceEventDecision()

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def observe_event(
        self, event: RawHarnessEvent, transition: str | None
    ) -> EvidenceEventDecision:
        del event, transition
        return EvidenceEventDecision()

    def note_event_persisted(self, event: RawHarnessEvent) -> EvidenceEventDecision:
        del event
        return self.persisted

    async def assess(self, trigger: AssessmentTrigger) -> WorkAssessment:
        del trigger
        if self.assessments:
            self.last = self.assessments.popleft()
        return self.last

    def next_due_at(self) -> float | None:
        return None

    async def handle_due(self) -> EvidenceEventDecision:
        return EvidenceEventDecision()

    def wants_aux_wake(self) -> bool:
        return False

    async def wait_for_change(self) -> None:
        return


class _Profile:
    def __init__(
        self,
        *,
        hold: bool = False,
        stabilization: float = 0.0,
        candidate_free: bool = False,
    ) -> None:
        self.hold = hold
        self.stabilization = stabilization
        self.candidate_free = candidate_free
        self.deadline_at: float | None = None
        self.done = False
        self.directives: deque[CompletionDirectives] = deque()

    def allows_evaluation_without_candidate(self) -> bool:
        return self.candidate_free

    def consume_directives(
        self, state: CompletionState, trigger: AssessmentTrigger
    ) -> CompletionDirectives:
        del state, trigger
        current = self.directives.popleft() if self.directives else CompletionDirectives()
        self.done = self.done or current.done
        return CompletionDirectives(done=self.done, rearm=current.rearm)

    def evaluate(self, context: CompletionEvaluation) -> ProfileDecision:
        candidate = context.candidate or context.terminal_outcome
        if candidate is None:
            return ProfileDecision(action="wait")
        if context.evidence_failure is not None:
            return ProfileDecision(action="fail", outcome=_FAILURE)
        if context.directives.done and context.assessment.disposition != "unknown":
            return ProfileDecision(action="complete", outcome=candidate)
        if context.directives.rearm:
            self.hold = True
            self.deadline_at = context.now + 10.0
            return ProfileDecision(action="wait", reset_deadline=True)
        if context.deadline_expired:
            return ProfileDecision(action="cleanup", outcome=_TIMEOUT, cleanup_reason="deadline")
        if context.evidence_activity is not None:
            return ProfileDecision(action="stabilize", restart_stabilization=True)
        if context.state.phase == "stabilizing":
            if (
                context.assessment.disposition == "ready"
                and context.assessment.generation == context.state.stabilization_generation
                and context.stabilization_elapsed
            ):
                return ProfileDecision(action="complete", outcome=candidate)
            return ProfileDecision(action="wait")
        if context.assessment.disposition != "ready" or self.hold:
            return ProfileDecision(action="wait")
        return ProfileDecision(
            action="stabilize" if self.stabilization else "complete",
            outcome=candidate if not self.stabilization else None,
        )

    def deadline_for(self, decision: ProfileDecision, now: float) -> float | None:
        if decision.action not in {"wait", "stabilize"}:
            return None
        if self.deadline_at is None:
            self.deadline_at = now + 10.0
        return self.deadline_at

    def stabilization_seconds(self) -> float:
        return self.stabilization

    def close_outcome(
        self, state: CompletionState, intentional_stop: bool
    ) -> TerminalEventOutcome | None:
        return state.candidate if intentional_stop else None

    def next_nudge_at(
        self, state: CompletionState, assessment: WorkAssessment
    ) -> float | None:
        del state, assessment
        return None

    async def send_nudge(self, urgency: NudgeUrgency) -> None:
        del urgency

    def stream_exit_decision(
        self,
        state: CompletionState,
        recorded_outcome: TerminalEventOutcome | None,
    ) -> ProfileExitDecision:
        del state
        return ProfileExitDecision(recorded_outcome=recorded_outcome)


class _RetainingStabilizationProfile(_Profile):
    def evaluate(self, context: CompletionEvaluation) -> ProfileDecision:
        if context.state.phase == "stabilizing" and context.trigger == "aux_wake":
            return ProfileDecision(action="hold_stabilization")
        if context.state.phase == "stabilizing" and context.trigger == "timeout":
            return ProfileDecision(action="abandon_candidate")
        return super().evaluate(context)


class _Cleanup:
    def __init__(self) -> None:
        self.calls: list[tuple[WorkAssessment, str]] = []

    async def cleanup(self, assessment: WorkAssessment, reason: str) -> CleanupReport:
        self.calls.append((assessment, reason))
        return CleanupReport(attempted_categories=("fake",))


def _coordinator(
    clock: FakeClock, evidence: _Evidence, profile: _Profile
) -> tuple[CompletionCoordinator, _Cleanup]:
    cleanup = _Cleanup()
    return (
        CompletionCoordinator(
            evidence=evidence, profile=profile, cleanup=cleanup, clock=clock.monotonic
        ),
        cleanup,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate_free", "expected_phase", "expected_deadline"),
    [(False, "running", None), (True, "waiting", 10.0)],
)
async def test_candidate_free_policy_controls_evaluation(
    candidate_free: bool,
    expected_phase: str,
    expected_deadline: float | None,
) -> None:
    clock = FakeClock()
    coordinator, _ = _coordinator(
        clock, _Evidence(_ready()), _Profile(candidate_free=candidate_free)
    )

    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is None
    assert coordinator.state.phase == expected_phase
    assert coordinator.deadline_monotonic == expected_deadline


@pytest.mark.asyncio
async def test_blocked_candidate_completes_after_fresh_readiness() -> None:
    clock = FakeClock()
    coordinator, _ = _coordinator(clock, _Evidence(_blocked(), _ready(2)), _Profile())

    waiting = await coordinator.handle_terminal_event(None, _SUCCESS, _TERMINATE)  # type: ignore[arg-type]
    completed = await coordinator.handle_timeout()

    assert waiting.recorded_outcome is None
    assert completed.recorded_outcome == _SUCCESS


@pytest.mark.asyncio
async def test_done_directive_waits_through_unknown_evidence() -> None:
    clock = FakeClock()
    profile = _Profile()
    profile.directives.append(CompletionDirectives(done=True))
    coordinator, _ = _coordinator(clock, _Evidence(_unknown(), _ready(2)), profile)

    waiting = await coordinator.handle_terminal_event(None, _SUCCESS, _TERMINATE)  # type: ignore[arg-type]
    completed = await coordinator.handle_timeout()

    assert waiting.recorded_outcome is None
    assert completed.recorded_outcome == _SUCCESS


@pytest.mark.asyncio
async def test_rearm_replaces_an_expired_deadline() -> None:
    clock = FakeClock()
    profile = _Profile()
    coordinator, cleanup = _coordinator(clock, _Evidence(_blocked()), profile)
    await coordinator.handle_terminal_event(None, _SUCCESS, _TERMINATE)  # type: ignore[arg-type]
    clock.advance(10.0)
    profile.directives.append(CompletionDirectives(rearm=True))

    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is None
    assert coordinator.deadline_monotonic == 20.0
    assert cleanup.calls == []


@pytest.mark.asyncio
async def test_stabilization_completes_after_unchanged_ready_recheck() -> None:
    clock = FakeClock()
    coordinator, _ = _coordinator(
        clock, _Evidence(_ready(7), _ready(7)), _Profile(stabilization=2.0)
    )

    candidate = await coordinator.handle_terminal_event(None, _SUCCESS, _TERMINATE)  # type: ignore[arg-type]
    clock.advance(2.0)
    completed = await coordinator.handle_timeout()

    assert candidate.recorded_outcome is None
    assert completed.recorded_outcome == _SUCCESS


@pytest.mark.asyncio
async def test_stabilization_can_hold_then_abandon_candidate() -> None:
    clock = FakeClock()
    coordinator, _ = _coordinator(
        clock,
        _Evidence(_ready(7), _blocked(8), _blocked(8)),
        _RetainingStabilizationProfile(stabilization=2.0),
    )
    await coordinator.handle_terminal_event(None, _SUCCESS, _TERMINATE)  # type: ignore[arg-type]
    stabilization_at = coordinator.state.stabilization_at

    clock.advance(1.0)
    await coordinator.handle_aux_wake()
    assert coordinator.state.stabilization_at == stabilization_at
    assert coordinator.pending_outcome == _SUCCESS

    clock.advance(1.0)
    await coordinator.handle_timeout()
    assert coordinator.state.phase == "waiting"
    assert coordinator.pending_outcome is None


@pytest.mark.asyncio
async def test_changed_stabilization_generation_restarts_from_waiting() -> None:
    clock = FakeClock()
    coordinator, _ = _coordinator(
        clock,
        _Evidence(_ready(7), _ready(8), _ready(8)),
        _Profile(stabilization=2.0),
    )
    await coordinator.handle_terminal_event(None, _SUCCESS, _TERMINATE)  # type: ignore[arg-type]

    clock.advance(1.0)
    await coordinator.after_event()
    assert coordinator.state.phase == "waiting"
    assert coordinator.state.stabilization_at is None

    await coordinator.handle_timeout()
    assert coordinator.state.phase == "stabilizing"
    assert coordinator.state.stabilization_generation == 8


@pytest.mark.asyncio
async def test_persisted_activity_restarts_stabilization_window() -> None:
    clock = FakeClock()
    evidence = _Evidence(_ready(7), _ready(7))
    evidence.persisted = EvidenceEventDecision(
        activity=EvidenceActivity(code="persisted_event")
    )
    coordinator, _ = _coordinator(clock, evidence, _Profile(stabilization=2.0))
    await coordinator.handle_terminal_event(None, _SUCCESS, _TERMINATE)  # type: ignore[arg-type]

    clock.advance(1.0)
    coordinator.note_event_persisted(None)  # type: ignore[arg-type]
    await coordinator.handle_aux_wake()

    assert coordinator.state.phase == "stabilizing"
    assert coordinator.state.stabilization_generation == 7
    assert coordinator.state.stabilization_at == 3.0


@pytest.mark.asyncio
async def test_persisted_evidence_failure_overrides_candidate() -> None:
    clock = FakeClock()
    evidence = _Evidence(_ready())
    evidence.persisted = EvidenceEventDecision(
        failure=EvidenceFailure(code="lifecycle_schema_invalid")
    )
    coordinator, _ = _coordinator(clock, evidence, _Profile(stabilization=2.0))
    await coordinator.handle_terminal_event(None, _SUCCESS, _TERMINATE)  # type: ignore[arg-type]

    decision = coordinator.note_event_persisted(None)  # type: ignore[arg-type]

    assert decision.recorded_outcome == _FAILURE


@pytest.mark.asyncio
async def test_deadline_cleanup_latches_once_after_publication() -> None:
    clock = FakeClock()
    coordinator, cleanup = _coordinator(clock, _Evidence(_blocked(), _ready(2)), _Profile())
    await coordinator.handle_terminal_event(None, _SUCCESS, _TERMINATE)  # type: ignore[arg-type]
    clock.advance(10.0)

    expired = await coordinator.handle_timeout()
    repeated = await coordinator.handle_timeout()
    exit_decision = await coordinator.handle_stream_exit(expired.recorded_outcome)
    request = exit_decision.post_publication_cleanup
    assert request is not None
    await coordinator.execute_post_publication_cleanup(request)
    await coordinator.execute_post_publication_cleanup(request)

    assert expired.recorded_outcome == _TIMEOUT
    assert repeated.recorded_outcome is None
    assert len(cleanup.calls) == 1
    assert coordinator.state.phase == "finalized"


def test_work_assessment_rejects_invalid_ready_blockers() -> None:
    with pytest.raises(ValueError):
        WorkAssessment(disposition="ready", blockers=_blocked().blockers, generation=1)
