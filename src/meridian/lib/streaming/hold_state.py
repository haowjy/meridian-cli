"""The resident hold's lifecycle: one state value, one precedence table and the folds.

No I/O, no async, no clock. `_HoldProfile` in `resident_drain.py` reads the files and the
clock, sends, and applies what these functions return. Why the rows are as they are:
`.context/completion-and-entry-points.md`. `decide` marks each row's name.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.harness.semantics import TerminalEventOutcome, TerminalOutcomeCause
from meridian.lib.streaming.completion_contracts import (
    COMPLETION_NUDGE_INTERVAL_SECONDS,
    CompletionState,
    DiagnosticBlocker,
    FinishedChild,
    ProfileAction,
    ProfileDecision,
    WorkAssessment,
)
from meridian.lib.streaming.drain_policy import DrainAction

_UNREADABLE = TerminalEventOutcome(
    status=SpawnStatus.FAILED, exit_code=1, error="resident_evidence_unreadable"
)
IDLE_NUDGE_ANSWER_SECONDS = 300.0  # how long a child gets to work after an expiry ask
_TURN_CUT_OFF = "transport_closed_during_turn"
_STREAM_CLOSED = TerminalEventOutcome(
    status=SpawnStatus.FAILED, exit_code=1, error="stream_closed_while_awaiting_done"
)
BACKEND_DEAD_OUTCOME = TerminalEventOutcome(
    status=SpawnStatus.FAILED, exit_code=1, error="backend_dead_while_awaiting_done"
)


class AskPhase(StrEnum):
    UNASKED = "unasked"  # nothing asked since the last reset
    REFUSED = "refused"  # an expiry ask was refused, so nothing reached the child; `answer` is open
    SENT = "sent"  # sent; a turn should start within the grace
    SETTLED = "settled"  # a turn started, the grace passed, or the send failed


# The child has seen no ask, so a kept candidate is not yet confirmed.
_NOTHING_ASKED = frozenset({AskPhase.UNASKED, AskPhase.REFUSED})


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
    asked_at: float | None = None  # when the last send attempt ended; survives a rearm
    answer: AnswerWindow | None = None
    lease: HeldLease | SpacedLease | None = None
    rearmed: bool = False  # the next good turn end is the next candidate
    closing: bool = False  # `done` latched
    stays: bool = False  # a rearm stands: only an ask can end it, even where turn ends skip one
    woken: frozenset[str] = frozenset()  # owed launches a wake has named


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
    rearm: bool  # signalled and within budget; only row B2 charges it
    dead: bool  # connection health says BACKEND_DEAD
    accepts_signals: bool
    skips_ask: bool  # the harness reports its own work, so a turn end needs no confirmation
    unread: tuple[FinishedChild, ...]  # finished children the parent has not consumed
    grace_seconds: float
    timeout: ProfileDecision  # RearmWindow.timeout_decision(), so the budget error stays there


@dataclass(frozen=True)
class HoldStep:
    state: HoldState
    decision: ProfileDecision
    leave_awaiting: bool = False  # D2, E2, E3 away from a turn end change nothing
    wake: tuple[FinishedChild, ...] = ()  # set on a nudge: name these, a wake and not an ask


def decide(state: HoldState, facts: HoldFacts) -> HoldStep:
    """Run the table: the first matching row wins."""
    # Timers past due end here, so neither can stay a wake.
    if state.ask is AskPhase.SENT and facts.now >= _grace_ends(state, facts.grace_seconds):
        state = replace(state, ask=AskPhase.SETTLED)
    if isinstance(state.lease, SpacedLease) and facts.now >= state.lease.not_before:
        state = replace(state, lease=None)
    end = facts.end
    disposition = facts.assessment.disposition

    # A1
    if end is not None and not end.action.terminate:
        decision = ProfileDecision(action="clear", emit_turn_boundary=end.action.emit_turn_boundary)
        return HoldStep(HoldState(), decision)
    if end is not None and end.outcome.status != "succeeded":
        if (
            facts.candidate is not None
            and end.outcome.cause is TerminalOutcomeCause.REPLACEABLE_TRANSPORT_CLOSE
        ):
            if disposition == "ready" and not facts.active_turn:  # A2
                return _publish(state, facts)
            if facts.expired:  # A3
                return _timeout(facts)
            if facts.active_turn:  # A3b
                # The turn outdated the candidate, and nothing that follows can vouch for
                # it, so no later row may publish: end here, without orphaning descendants.
                decision = ProfileDecision(
                    action="cleanup", outcome=end.outcome, cleanup_reason=_TURN_CUT_OFF
                )
                return HoldStep(HoldState(), decision)
            if disposition == "unknown" or _descendant_blocked(facts.assessment):  # A4
                ask = AskPhase.SETTLED if state.ask is AskPhase.SENT else state.ask
                return _wait(replace(state, ask=ask), facts)
        decision = ProfileDecision(action="fail", outcome=end.outcome)  # A5
        return HoldStep(HoldState(), decision)
    if state.answer is not None and facts.dead:  # B1
        return _timeout(facts)
    if facts.rearm:  # B2
        decision = ProfileDecision(
            action="abandon_candidate", emit_turn_boundary=end is not None, reset_deadline=True
        )
        return HoldStep(_after_rearm(state, facts.assessment), decision)
    if facts.candidate is None:
        if facts.expired:  # C1
            return _expiry(state, facts)
        return _wake(state, facts) or _wait(state, facts)  # C1b, C2
    if state.closing:
        if disposition == "unknown":
            if facts.expired and end is None:  # D1
                decision = ProfileDecision(action="fail", outcome=_UNREADABLE)
                return HoldStep(HoldState(), decision)
            return _wait(state, facts, leave_awaiting=end is None)  # D2
        if not facts.active_turn:  # D3
            return _publish(state, facts)
    if facts.expired:  # E1
        return _expiry(state, facts)
    if end is not None:
        state = replace(state, rearmed=False)
    if facts.active_turn:  # E2
        return _wait(state, facts, leave_awaiting=end is None)
    if disposition == "unknown":  # E3
        return _wait(state, facts, leave_awaiting=end is None)
    wake = _wake(state, facts)  # E3b
    if wake is not None:
        return wake
    # E4
    if (
        facts.accepts_signals
        and (state.stays or not facts.skips_ask)
        and not state.rearmed
        and (state.ask is AskPhase.UNASKED or (state.ask is AskPhase.REFUSED and end is not None))
        and not _lease_quiet(state.lease, facts.assessment)
    ):
        return _wait(replace(state, lease=None), facts, nudge=True)
    if disposition == "ready" and _settled(state, facts):  # E5
        return _publish(state, facts)
    return _wait(state, facts)  # E6


def outcome_at_close(
    state: CompletionState, *, intentional_stop: bool, dead: bool
) -> TerminalEventOutcome | None:
    """What a closed stream publishes. A turn still running outdates the candidate."""
    if state.candidate is None:
        return None
    assessment = state.assessment
    if assessment is not None and assessment.disposition == "ready" and not state.active_turn:
        return state.candidate
    return BACKEND_DEAD_OUTCOME if dead and not intentional_stop else _STREAM_CLOSED


def defers_close(hold: HoldState, state: CompletionState, *, dead: bool) -> bool:
    """Keep waiting on a closed stream? A dead backend will not speak again."""
    if dead:
        return False
    if hold.rearmed:
        return True
    return (
        state.candidate is not None
        and state.assessment is not None
        and _descendant_blocked(state.assessment)
    )


def next_deadline(
    state: HoldState, action: ProfileAction, *, now: float, window_ends: float
) -> float | None:
    """The idle deadline a decision sets; an open `answer` gets its own, shorter one."""
    if action not in {"wait", "complete", "abandon_candidate"}:
        return None
    return window_ends if state.answer is None else now + IDLE_NUDGE_ANSWER_SECONDS


def next_wake(state: HoldState, grace_seconds: float, *, has_candidate: bool) -> float | None:
    """When the table next changes its answer with no event: both timers end in `decide`."""
    due: list[float] = []
    if state.ask is AskPhase.SENT:
        due.append(_grace_ends(state, grace_seconds))
    if has_candidate and isinstance(state.lease, SpacedLease):
        due.append(state.lease.not_before)
    return min(due, default=None)


def after_signals(state: HoldState, *, done: bool, rearm: bool) -> tuple[HoldState, bool]:
    """Fold one read of the signal files; return the state and whether a rearm stands.

    `rearm` means signalled and within budget. A signal needs no candidate: the files are
    read at every turn end and evaluation, so what they hold was written since the last
    read, in the turn the next candidate comes from.
    """
    if rearm:
        return replace(state, closing=False), True
    return replace(state, closing=state.closing or done), False


def after_event(state: HoldState, *, turn_started: bool, work_in_turn: bool) -> HoldState:
    """Fold one harness event: a turn settles an ask, work in a turn closes the window."""
    if turn_started and state.ask is AskPhase.SENT:
        state = replace(state, ask=AskPhase.SETTLED)
    if work_in_turn:
        # The refusal only meant something while its window was open.
        ask = AskPhase.UNASKED if state.ask is AskPhase.REFUSED else state.ask
        state = replace(state, ask=ask, answer=None)
    return state


def after_send(state: HoldState, result: SendResult, *, now: float) -> HoldState:
    """Fold one send attempt at the time it ended. An open `answer` means expiry kind."""
    state = replace(state, asked_at=now)
    if result is SendResult.SENT:
        answer = None if state.answer is None else AnswerWindow(retry_left=False)
        return replace(state, ask=AskPhase.SENT, answer=answer)
    if result is SendResult.DEAD and state.answer is not None:
        state = replace(state, answer=AnswerWindow(retry_left=False))
    if result is SendResult.REFUSED and state.answer is not None and state.ask in _NOTHING_ASKED:
        return replace(state, ask=AskPhase.REFUSED)
    return replace(state, ask=AskPhase.SETTLED)


def _wait(
    state: HoldState, facts: HoldFacts, *, nudge: bool = False, leave_awaiting: bool = False
) -> HoldStep:
    decision = ProfileDecision(action="wait", emit_turn_boundary=facts.end is not None, nudge=nudge)
    return HoldStep(state, decision, leave_awaiting)


def _publish(state: HoldState, facts: HoldFacts) -> HoldStep:
    assert facts.candidate is not None
    return HoldStep(state, ProfileDecision(action="complete", outcome=facts.candidate))


def _timeout(facts: HoldFacts) -> HoldStep:
    return HoldStep(HoldState(), facts.timeout)


def _expiry(state: HoldState, facts: HoldFacts) -> HoldStep:
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
        return HoldStep(replace(state, answer=answer), decision)
    return _timeout(facts)


def _wake(state: HoldState, facts: HoldFacts) -> HoldStep | None:
    """Name what the parent is owed, once each, when nothing runs and no turn will come."""
    if (
        not facts.accepts_signals
        or facts.active_turn
        or state.ask is AskPhase.SENT
        or facts.assessment.disposition != "ready"
    ):
        return None
    wake = tuple(child for child in facts.unread if child.key not in state.woken)
    if not wake:
        return None
    # Marked here, whatever the send does: a failed send re-evaluates at once.
    woken = state.woken | {child.key for child in wake}
    step = _wait(replace(state, woken=woken), facts, nudge=True)
    return replace(step, wake=wake)


def _settled(state: HoldState, facts: HoldFacts) -> bool:
    # A refused expiry ask: a ready tree may still publish.
    return (
        not facts.accepts_signals
        or (facts.skips_ask and not state.stays and state.ask is AskPhase.UNASKED)
        or state.ask in {AskPhase.SETTLED, AskPhase.REFUSED}
    )


def _grace_ends(state: HoldState, grace_seconds: float) -> float:
    assert state.asked_at is not None
    return state.asked_at + grace_seconds


def _lease_quiet(lease: HeldLease | SpacedLease | None, assessment: WorkAssessment) -> bool:
    if isinstance(lease, HeldLease):
        blockers = assessment.blockers
        held = (b in lease.blockers or b.code == "awaiting_followup_result" for b in blockers)
        return bool(blockers) and all(held)
    return lease is not None  # `decide` retires a SpacedLease at `not_before`


def _after_rearm(state: HoldState, assessment: WorkAssessment) -> HoldState:
    blockers = frozenset(assessment.blockers)
    lease: HeldLease | SpacedLease | None = None
    if blockers:
        lease = HeldLease(blockers)
    elif state.asked_at is not None:
        lease = SpacedLease(state.asked_at + COMPLETION_NUDGE_INTERVAL_SECONDS)
    return HoldState(
        asked_at=state.asked_at, lease=lease, rearmed=True, stays=True, woken=state.woken
    )


def _descendant_blocked(assessment: WorkAssessment) -> bool:
    return assessment.disposition == "blocked" and any(
        blocker.source == "persisted_descendant" for blocker in assessment.blockers
    )


__all__ = [
    "BACKEND_DEAD_OUTCOME",
    "IDLE_NUDGE_ANSWER_SECONDS",
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
    "after_signals",
    "decide",
    "defers_close",
    "next_deadline",
    "next_wake",
    "outcome_at_close",
]
