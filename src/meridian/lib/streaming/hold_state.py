"""The resident hold's ask lifecycle: one state value and one precedence table.

No I/O, no async, no clock. `_HoldProfile` in `resident_drain.py` gathers the facts,
calls `decide` and applies the step. The rows are named as in
`.context/completion-and-entry-points.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.harness.semantics import TerminalEventOutcome, TerminalOutcomeCause
from meridian.lib.streaming.completion_contracts import (
    COMPLETION_NUDGE_INTERVAL_SECONDS,
    DiagnosticBlocker,
    ProfileDecision,
    WorkAssessment,
)
from meridian.lib.streaming.drain_policy import DrainAction

_UNREADABLE = TerminalEventOutcome(
    status=SpawnStatus.FAILED, exit_code=1, error="resident_evidence_unreadable"
)


class AskPhase(StrEnum):
    UNASKED = "unasked"  # nothing asked since the last reset
    SENT = "sent"  # sent; a turn should start within the grace
    SETTLED = "settled"  # a turn started, the grace passed, or the send failed


class SendResult(StrEnum):
    SENT = "sent"  # delivered, or may have arrived
    REFUSED = "refused"  # alive, nothing written
    DEAD = "dead"  # gone, nothing written


@dataclass(frozen=True)
class AnswerWindow:
    """Open from an expiry ask until the child works."""

    retry_left: bool


@dataclass(frozen=True)
class HeldLease:
    """Turn ends stay quiet while these blockers are all that runs."""

    blockers: frozenset[DiagnosticBlocker]


@dataclass(frozen=True)
class SpacedLease:
    """Rearm with nothing visible: the next ask waits for this time."""

    not_before: float


@dataclass(frozen=True)
class HoldState:
    """ "Reset" means `HoldState()`."""

    ask: AskPhase = AskPhase.UNASKED
    asked_at: float | None = None  # last send; survives a rearm
    answer: AnswerWindow | None = None
    lease: HeldLease | SpacedLease | None = None
    rearmed: bool = False  # the next good turn end is the next candidate
    closing: bool = False  # `done` latched


@dataclass(frozen=True)
class TurnEnd:
    outcome: TerminalEventOutcome
    action: DrainAction


@dataclass(frozen=True)
class HoldFacts:
    now: float
    end: TurnEnd | None
    active_turn: bool
    expired: bool
    candidate: TerminalEventOutcome | None
    assessment: WorkAssessment
    rearm: bool  # already granted by RearmWindow
    dead: bool  # connection health says BACKEND_DEAD
    accepts_signals: bool
    grace_seconds: float
    timeout: ProfileDecision  # RearmWindow.timeout_decision(), so the budget error stays there


@dataclass(frozen=True)
class HoldStep:
    state: HoldState
    decision: ProfileDecision
    enter_wait: bool
    row: str


def decide(state: HoldState, facts: HoldFacts) -> HoldStep:
    """Run the table: the first matching row wins."""
    if state.ask is AskPhase.SENT and facts.now >= grace_ends(state, facts.grace_seconds):
        state = replace(state, ask=AskPhase.SETTLED)
    end = facts.end
    disposition = facts.assessment.disposition

    if end is not None and not end.action.terminate:
        decision = ProfileDecision(action="clear", emit_turn_boundary=end.action.emit_turn_boundary)
        return HoldStep(HoldState(), decision, False, "A1")
    if end is not None and end.outcome.status != "succeeded":
        if (
            facts.candidate is not None
            and end.outcome.cause is TerminalOutcomeCause.REPLACEABLE_TRANSPORT_CLOSE
        ):
            if disposition == "ready":
                return _publish("A2", state, facts)
            if facts.expired:
                return _timeout("A3", facts)
            if disposition == "unknown" or _descendant_blocked(facts.assessment):
                ask = AskPhase.UNASKED if state.ask is AskPhase.UNASKED else AskPhase.SETTLED
                return _wait("A4", replace(state, ask=ask), facts)
        decision = ProfileDecision(action="fail", outcome=end.outcome)
        return HoldStep(HoldState(), decision, False, "A5")
    if state.answer is not None and facts.dead:
        return _timeout("B1", facts)
    if facts.rearm:
        decision = ProfileDecision(
            action="abandon_candidate", emit_turn_boundary=end is not None, reset_deadline=True
        )
        return HoldStep(_after_rearm(state, facts.assessment), decision, True, "B2")
    if facts.candidate is None:
        return _expiry("C1", state, facts) if facts.expired else _wait("C2", state, facts)
    if state.closing:
        if disposition == "unknown":
            if facts.expired and end is None:
                decision = ProfileDecision(action="fail", outcome=_UNREADABLE)
                return HoldStep(HoldState(), decision, False, "D1")
            return _wait("D2", state, facts, enter=end is not None)
        if not facts.active_turn:
            return _publish("D3", state, facts)
    if facts.expired:
        return _expiry("E1", state, facts)
    if end is not None:
        state = replace(state, rearmed=False)
    if facts.active_turn:
        return _wait("E2", state, facts, enter=end is not None)
    if disposition == "unknown":
        return _wait("E3", state, facts, enter=end is not None)
    if (
        facts.accepts_signals
        and state.ask is AskPhase.UNASKED
        and not state.rearmed
        and (state.answer is None or end is not None)
    ):
        quiet, state = _lease_quiet(state, facts)
        if not quiet:
            return _wait("E4", state, facts, nudge=True)
    if disposition == "ready" and _settled(state, facts):
        return _publish("E5", state, facts)
    return _wait("E6", state, facts)


def grace_ends(state: HoldState, grace_seconds: float) -> float:
    assert state.asked_at is not None
    return state.asked_at + grace_seconds


def after_event(state: HoldState, *, turn_started: bool, work_in_turn: bool) -> HoldState:
    """Fold one harness event: a turn settles an ask, work in a turn closes the window."""
    if turn_started and state.ask is not AskPhase.UNASKED:
        state = replace(state, ask=AskPhase.SETTLED)
    if work_in_turn:
        state = replace(state, answer=None)
    return state


def after_send(state: HoldState, result: SendResult, *, prior: AskPhase, now: float) -> HoldState:
    """Fold one send. `prior` is the phase before it; an open `answer` means expiry kind."""
    if result is SendResult.SENT:
        answer = None if state.answer is None else AnswerWindow(retry_left=False)
        return replace(state, ask=AskPhase.SENT, asked_at=now, answer=answer)
    if result is SendResult.DEAD and state.answer is not None:
        state = replace(state, answer=AnswerWindow(retry_left=False))
    if result is SendResult.REFUSED and state.answer is not None and prior is AskPhase.UNASKED:
        return replace(state, ask=AskPhase.UNASKED)
    return replace(state, ask=AskPhase.SETTLED)


def _wait(
    row: str, state: HoldState, facts: HoldFacts, *, nudge: bool = False, enter: bool = True
) -> HoldStep:
    decision = ProfileDecision(action="wait", emit_turn_boundary=facts.end is not None, nudge=nudge)
    return HoldStep(state, decision, enter, row)


def _publish(row: str, state: HoldState, facts: HoldFacts) -> HoldStep:
    assert facts.candidate is not None
    return HoldStep(state, ProfileDecision(action="complete", outcome=facts.candidate), True, row)


def _timeout(row: str, facts: HoldFacts) -> HoldStep:
    return HoldStep(HoldState(), facts.timeout, False, row)


def _expiry(row: str, state: HoldState, facts: HoldFacts) -> HoldStep:
    """Ask once, then clean up if the child does not work in the answer window."""
    if (
        facts.accepts_signals
        and not facts.dead
        and (state.answer is None or state.answer.retry_left)
    ):
        decision = ProfileDecision(
            action="wait",
            emit_turn_boundary=facts.end is not None,
            nudge=True,
            reset_deadline=True,
        )
        answer = AnswerWindow(retry_left=state.answer is None)
        return HoldStep(replace(state, answer=answer), decision, True, row)
    return _timeout(row, facts)


def _settled(state: HoldState, facts: HoldFacts) -> bool:
    if not facts.accepts_signals or state.ask is AskPhase.SETTLED:
        return True
    # An expiry ask that was refused: a ready tree may still publish.
    return state.ask is AskPhase.UNASKED and state.answer is not None


def _lease_quiet(state: HoldState, facts: HoldFacts) -> tuple[bool, HoldState]:
    lease = state.lease
    if isinstance(lease, HeldLease):
        blockers = facts.assessment.blockers
        if blockers and all(
            blocker in lease.blockers or blocker.code == "awaiting_followup_result"
            for blocker in blockers
        ):
            return True, state
        return False, replace(state, lease=None)
    if isinstance(lease, SpacedLease):
        return facts.now < lease.not_before, state
    return False, state


def _after_rearm(state: HoldState, assessment: WorkAssessment) -> HoldState:
    blockers = frozenset(assessment.blockers)
    lease: HeldLease | SpacedLease | None = None
    if blockers:
        lease = HeldLease(blockers)
    elif state.asked_at is not None:
        lease = SpacedLease(state.asked_at + COMPLETION_NUDGE_INTERVAL_SECONDS)
    return HoldState(asked_at=state.asked_at, lease=lease, rearmed=True)


def _descendant_blocked(assessment: WorkAssessment) -> bool:
    return assessment.disposition == "blocked" and any(
        blocker.source == "persisted_descendant" for blocker in assessment.blockers
    )


__all__ = [
    "AnswerWindow",
    "AskPhase",
    "HeldLease",
    "HoldFacts",
    "HoldState",
    "HoldStep",
    "SendResult",
    "SpacedLease",
    "TurnEnd",
    "after_event",
    "after_send",
    "decide",
    "grace_ends",
]
