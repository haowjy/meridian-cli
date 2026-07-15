"""Table coverage for the profile-driven completion state machine."""

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
    from meridian.lib.harness.connections.base import HarnessEvent

_SUCCESS = TerminalEventOutcome(status="succeeded", exit_code=0)
_TIMEOUT = TerminalEventOutcome(status="timed_out", exit_code=1, error="deadline")
_EVIDENCE_FAILURE = TerminalEventOutcome(
    status="failed",
    exit_code=1,
    error="evidence_failure",
)
_TERMINATE = DrainAction(terminate=True, emit_turn_boundary=False)


def _ready(generation: int = 1) -> WorkAssessment:
    return WorkAssessment(disposition="ready", blockers=(), generation=generation)


def _blocked(generation: int = 1) -> WorkAssessment:
    return WorkAssessment(
        disposition="blocked",
        blockers=(
            DiagnosticBlocker(
                source="persisted_descendant",
                code="active",
                identity="p2",
            ),
        ),
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
        self.assess_calls: list[AssessmentTrigger] = []
        self.persisted_decisions: deque[EvidenceEventDecision] = deque()

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def observe_event(
        self, event: HarnessEvent, transition: str | None
    ) -> EvidenceEventDecision:
        del event, transition
        return EvidenceEventDecision()

    def note_event_persisted(self, event: HarnessEvent) -> EvidenceEventDecision:
        del event
        if self.persisted_decisions:
            return self.persisted_decisions.popleft()
        return EvidenceEventDecision()

    async def assess(self, trigger: AssessmentTrigger) -> WorkAssessment:
        self.assess_calls.append(trigger)
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
        deadline_seconds: float = 10.0,
        stabilization_seconds: float = 0.0,
        hold: bool = False,
    ) -> None:
        self.deadline_seconds = deadline_seconds
        self.stabilization = stabilization_seconds
        self.hold = hold
        self.deadline_at: float | None = None
        self.done_requested = False
        self.directives: deque[CompletionDirectives] = deque()
        self.evaluations: list[CompletionEvaluation] = []

    def consume_directives(
        self,
        state: CompletionState,
        trigger: AssessmentTrigger,
    ) -> CompletionDirectives:
        del state, trigger
        current = self.directives.popleft() if self.directives else CompletionDirectives()
        self.done_requested = self.done_requested or current.done
        return CompletionDirectives(done=self.done_requested, rearm=current.rearm)

    def evaluate(self, context: CompletionEvaluation) -> ProfileDecision:
        self.evaluations.append(context)
        candidate = context.candidate or context.terminal_outcome
        assert candidate is not None
        if context.evidence_failure is not None:
            return ProfileDecision(action="fail", outcome=_EVIDENCE_FAILURE)
        if context.directives.done and context.assessment.disposition != "unknown":
            return ProfileDecision(action="complete", outcome=candidate)
        rearmed = context.directives.rearm
        if rearmed:
            self.hold = True
            self.deadline_at = context.now + self.deadline_seconds
        if context.deadline_expired and not rearmed:
            return ProfileDecision(
                action="cleanup",
                outcome=_TIMEOUT,
                cleanup_reason="deadline",
            )
        if context.state.phase == "stabilizing":
            if context.assessment.disposition != "ready":
                return ProfileDecision(action="wait")
            if context.evidence_activity is not None:
                return ProfileDecision(action="stabilize", restart_stabilization=True)
            if context.assessment.generation != context.state.stabilization_generation:
                return ProfileDecision(action="wait")
            if (
                context.stabilization_elapsed
            ):
                return ProfileDecision(action="complete", outcome=candidate)
            return ProfileDecision(
                action="stabilize",
                restart_stabilization=context.trigger == "event",
            )
        if context.assessment.disposition != "ready" or self.hold:
            return ProfileDecision(action="wait", reset_deadline=rearmed)
        if self.stabilization > 0:
            return ProfileDecision(action="stabilize")
        return ProfileDecision(action="complete", outcome=candidate)

    def deadline_for(self, decision: ProfileDecision, now: float) -> float | None:
        if decision.action not in {"wait", "stabilize"}:
            return None
        if self.deadline_at is None:
            self.deadline_at = now + self.deadline_seconds
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


class _Cleanup:
    def __init__(self) -> None:
        self.calls: list[tuple[WorkAssessment, str]] = []

    async def cleanup(self, assessment: WorkAssessment, reason: str) -> CleanupReport:
        self.calls.append((assessment, reason))
        return CleanupReport(attempted_categories=("fake",))


class _RetainingStabilizationProfile(_Profile):
    def evaluate(self, context: CompletionEvaluation) -> ProfileDecision:
        if context.state.phase == "stabilizing" and context.trigger == "aux_wake":
            return ProfileDecision(action="hold_stabilization")
        if context.state.phase == "stabilizing" and context.trigger == "timeout":
            return ProfileDecision(action="abandon_candidate")
        return super().evaluate(context)


def _coordinator(
    clock: FakeClock,
    evidence: _Evidence,
    profile: _Profile,
    cleanup: _Cleanup | None = None,
) -> tuple[CompletionCoordinator, _Cleanup]:
    selected_cleanup = cleanup or _Cleanup()
    return (
        CompletionCoordinator(
            evidence=evidence,
            profile=profile,
            cleanup=selected_cleanup,
            clock=clock.monotonic,
        ),
        selected_cleanup,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("assessment", [_blocked(), _unknown()])
async def test_blocked_and_unknown_candidates_wait_for_fresh_readiness(
    assessment: WorkAssessment,
) -> None:
    clock = FakeClock(start=100.0)
    evidence = _Evidence(assessment, _ready(generation=2))
    coordinator, _ = _coordinator(clock, evidence, _Profile())

    candidate = await coordinator.handle_terminal_event(None, _SUCCESS, _TERMINATE)  # type: ignore[arg-type]
    assert candidate.recorded_outcome is None
    assert coordinator.state.phase == "waiting"

    completed = await coordinator.handle_timeout()
    assert completed.recorded_outcome == _SUCCESS
    assert evidence.assess_calls == ["terminal_candidate", "timeout"]


@pytest.mark.asyncio
async def test_candidate_hold_waits_until_done_directive() -> None:
    clock = FakeClock(start=20.0)
    evidence = _Evidence(_ready())
    profile = _Profile(hold=True)
    profile.directives.append(CompletionDirectives())
    profile.directives.append(CompletionDirectives(done=True))
    coordinator, _ = _coordinator(clock, evidence, profile)

    waiting = await coordinator.handle_terminal_event(None, _SUCCESS, _TERMINATE)  # type: ignore[arg-type]
    assert waiting.recorded_outcome is None
    assert coordinator.deadline_monotonic == 30.0

    done = await coordinator.handle_timeout()
    assert done.recorded_outcome == _SUCCESS


@pytest.mark.asyncio
@pytest.mark.parametrize("done_during", ["terminal", "waiting"])
async def test_done_waits_through_unknown_until_a_known_fresh_assessment(
    done_during: str,
) -> None:
    clock = FakeClock()
    assessments = (
        (_unknown(), _ready(generation=2))
        if done_during == "terminal"
        else (_blocked(), _unknown(generation=2), _ready(generation=3))
    )
    evidence = _Evidence(*assessments)
    profile = _Profile()
    if done_during == "terminal":
        profile.directives.append(CompletionDirectives(done=True))
    coordinator, _ = _coordinator(clock, evidence, profile)

    candidate = await coordinator.handle_terminal_event(None, _SUCCESS, _TERMINATE)  # type: ignore[arg-type]
    assert candidate.recorded_outcome is None
    if done_during == "waiting":
        profile.directives.append(CompletionDirectives(done=True))
        unknown = await coordinator.handle_timeout()
        assert unknown.recorded_outcome is None

    completed = await coordinator.handle_timeout()
    assert completed.recorded_outcome == _SUCCESS


@pytest.mark.asyncio
async def test_rearm_replaces_an_expired_deadline_before_profile_evaluation() -> None:
    clock = FakeClock()
    evidence = _Evidence(_blocked())
    profile = _Profile()
    coordinator, cleanup = _coordinator(clock, evidence, profile)
    await coordinator.handle_terminal_event(None, _SUCCESS, _TERMINATE)  # type: ignore[arg-type]
    clock.advance(10.0)
    profile.directives.append(CompletionDirectives(rearm=True))

    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is None
    assert coordinator.deadline_monotonic == 20.0
    assert cleanup.calls == []


@pytest.mark.asyncio
async def test_stabilization_requires_an_unchanged_fresh_ready_recheck() -> None:
    clock = FakeClock(start=5.0)
    evidence = _Evidence(
        _ready(generation=7),
        _ready(generation=7),
        _ready(generation=7),
    )
    profile = _Profile(stabilization_seconds=2.0)
    coordinator, _ = _coordinator(clock, evidence, profile)

    candidate = await coordinator.handle_terminal_event(None, _SUCCESS, _TERMINATE)  # type: ignore[arg-type]
    assert candidate.recorded_outcome is None
    assert coordinator.state.phase == "stabilizing"
    assert coordinator.next_timeout() == pytest.approx(2.0)

    clock.advance(1.0)
    early_wake = await coordinator.handle_aux_wake()
    assert early_wake.recorded_outcome is None
    assert coordinator.state.phase == "stabilizing"
    assert coordinator.next_timeout() == pytest.approx(1.0)

    clock.advance(1.0)
    completed = await coordinator.handle_timeout()
    assert completed.recorded_outcome == _SUCCESS
    assert evidence.assess_calls == ["terminal_candidate", "aux_wake", "timeout"]


@pytest.mark.asyncio
async def test_stabilization_can_hold_then_abandon_the_candidate() -> None:
    clock = FakeClock(start=5.0)
    evidence = _Evidence(_ready(generation=7), _blocked(generation=8), _blocked(generation=8))
    profile = _RetainingStabilizationProfile(stabilization_seconds=2.0)
    coordinator, _ = _coordinator(clock, evidence, profile)
    await coordinator.handle_terminal_event(None, _SUCCESS, _TERMINATE)  # type: ignore[arg-type]
    original_deadline = coordinator.state.stabilization_at

    clock.advance(1.0)
    await coordinator.handle_aux_wake()

    assert coordinator.state.phase == "stabilizing"
    assert coordinator.pending_outcome == _SUCCESS
    assert coordinator.state.stabilization_at == original_deadline

    clock.advance(1.0)
    await coordinator.handle_timeout()

    assert coordinator.state.phase == "waiting"
    assert coordinator.pending_outcome is None
    assert coordinator.state.stabilization_at is None


@pytest.mark.asyncio
async def test_changed_stabilization_generation_returns_to_waiting() -> None:
    clock = FakeClock()
    evidence = _Evidence(_ready(generation=7), _ready(generation=8), _ready(generation=8))
    profile = _Profile(stabilization_seconds=2.0)
    coordinator, _ = _coordinator(clock, evidence, profile)
    await coordinator.handle_terminal_event(None, _SUCCESS, _TERMINATE)  # type: ignore[arg-type]

    clock.advance(1.0)
    changed = await coordinator.after_event()
    assert changed.recorded_outcome is None
    assert coordinator.state.phase == "waiting"
    assert coordinator.state.stabilization_at is None

    restarted = await coordinator.handle_timeout()
    assert restarted.recorded_outcome is None
    assert coordinator.state.phase == "stabilizing"
    assert coordinator.state.stabilization_generation == 8


@pytest.mark.asyncio
async def test_persisted_event_restarts_same_generation_stabilization_window() -> None:
    clock = FakeClock()
    evidence = _Evidence(_ready(generation=7), _ready(generation=7))
    evidence.persisted_decisions.append(
        EvidenceEventDecision(activity=EvidenceActivity(code="persisted_event"))
    )
    profile = _Profile(stabilization_seconds=2.0)
    coordinator, _ = _coordinator(clock, evidence, profile)
    await coordinator.handle_terminal_event(None, _SUCCESS, _TERMINATE)  # type: ignore[arg-type]
    assert coordinator.state.stabilization_at == 2.0

    clock.advance(2.0)
    coordinator.note_event_persisted(None)  # type: ignore[arg-type]
    event = await coordinator.handle_aux_wake()

    assert event.recorded_outcome is None
    assert coordinator.state.phase == "stabilizing"
    assert coordinator.state.stabilization_at == 4.0
    assert coordinator.next_timeout() == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_post_persist_failure_is_mapped_by_profile_synchronously() -> None:
    clock = FakeClock()
    failure = EvidenceFailure(code="lifecycle_schema_invalid", detail="version 999")
    evidence = _Evidence(_ready())
    evidence.persisted_decisions.append(EvidenceEventDecision(failure=failure))
    profile = _Profile(stabilization_seconds=2.0)
    coordinator, _ = _coordinator(clock, evidence, profile)
    await coordinator.handle_terminal_event(None, _SUCCESS, _TERMINATE)  # type: ignore[arg-type]

    decision = coordinator.note_event_persisted(None)  # type: ignore[arg-type]

    assert decision.recorded_outcome == _EVIDENCE_FAILURE
    assert profile.evaluations[-1].evidence_failure == failure


@pytest.mark.asyncio
async def test_existing_deadline_is_not_replaced_on_an_ordinary_wait() -> None:
    clock = FakeClock()
    evidence = _Evidence(_blocked())
    profile = _Profile()
    coordinator, _ = _coordinator(clock, evidence, profile)
    coordinator.pending_outcome = _SUCCESS
    coordinator.deadline_monotonic = 10.0

    clock.advance(5.0)
    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is None
    assert coordinator.deadline_monotonic == 10.0


@pytest.mark.asyncio
async def test_deadline_cleanup_runs_once_and_timeout_wins_over_fresh_readiness() -> None:
    clock = FakeClock()
    evidence = _Evidence(_blocked(), _ready(generation=2))
    profile = _Profile()
    coordinator, cleanup = _coordinator(clock, evidence, profile)
    await coordinator.handle_terminal_event(None, _SUCCESS, _TERMINATE)  # type: ignore[arg-type]
    clock.advance(10.0)

    expired = await coordinator.handle_timeout()
    repeated = await coordinator.handle_timeout()

    assert expired.recorded_outcome == _TIMEOUT
    assert repeated.recorded_outcome is None
    assert len(cleanup.calls) == 1
    assert cleanup.calls[0][0].disposition == "ready"
    assert coordinator.deadline_monotonic is None
    assert coordinator.state.phase == "finalized"


@pytest.mark.parametrize(
    ("disposition", "blockers", "failure"),
    [
        ("ready", (_blocked().blockers[0],), None),
        ("blocked", (), None),
        ("unknown", (), None),
    ],
)
def test_work_assessment_rejects_invalid_combinations(
    disposition: str,
    blockers: tuple[DiagnosticBlocker, ...],
    failure: EvidenceFailure | None,
) -> None:
    with pytest.raises(ValueError):
        WorkAssessment(
            disposition=disposition,  # type: ignore[arg-type]
            blockers=blockers,
            generation=1,
            failure=failure,
        )
