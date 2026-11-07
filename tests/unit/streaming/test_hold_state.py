"""The hold's precedence table: one case per row and per send outcome."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.harness.semantics import TerminalEventOutcome, TerminalOutcomeCause
from meridian.lib.streaming.completion_contracts import (
    CompletionState,
    DiagnosticBlocker,
    EvidenceFailure,
    FinishedChild,
    ProfileAction,
    ProfileDecision,
    WorkAssessment,
)
from meridian.lib.streaming.drain_policy import DrainAction
from meridian.lib.streaming.hold_state import (
    BACKEND_DEAD_OUTCOME,
    AnswerWindow,
    AskPhase,
    HeldLease,
    HoldFacts,
    HoldState,
    SendResult,
    SpacedLease,
    TurnEnd,
    after_event,
    after_send,
    after_signals,
    decide,
    defers_close,
    next_deadline,
    next_wake,
    outcome_at_close,
)

GOOD = TerminalEventOutcome(status=SpawnStatus.SUCCEEDED, exit_code=0)
BAD = TerminalEventOutcome(status=SpawnStatus.FAILED, exit_code=1, error="boom")
CLOSED = TerminalEventOutcome(
    status=SpawnStatus.FAILED,
    exit_code=1,
    error="closed",
    cause=TerminalOutcomeCause.REPLACEABLE_TRANSPORT_CLOSE,
)
UNREADABLE = TerminalEventOutcome(
    status=SpawnStatus.FAILED, exit_code=1, error="resident_evidence_unreadable"
)
TIMEOUT = ProfileDecision(
    action="cleanup",
    outcome=TerminalEventOutcome(status=SpawnStatus.TIMED_OUT, exit_code=1, error="expired"),
    cleanup_reason="expired",
)
READY = WorkAssessment(disposition="ready", blockers=(), generation=0)
UNKNOWN = WorkAssessment(
    disposition="unknown", blockers=(), generation=0, failure=EvidenceFailure(code="unreadable")
)
CHILD = DiagnosticBlocker(source="persisted_descendant", code="running", identity="p2")
TASK = DiagnosticBlocker(source="native_task", code="running", identity="t1")
FOLLOWUP = DiagnosticBlocker(source="native_task", code="awaiting_followup_result")
BLOCKED = WorkAssessment(disposition="blocked", blockers=(CHILD,), generation=0)
FIRST = FinishedChild("p2:1", "p2", "succeeded", Path("/r/p2/report.md"))
SECOND = FinishedChild("p3:1", "p3", "failed", Path("/r/p3/report.md"))
TERMINATE = DrainAction(terminate=True, emit_turn_boundary=False)
GOOD_END = TurnEnd(GOOD, TERMINATE)
CLOSED_END = TurnEnd(CLOSED, TERMINATE)
OPEN = AnswerWindow(retry_left=True)
SPENT = AnswerWindow(retry_left=False)
SENT = HoldState(ask=AskPhase.SENT, asked_at=99.0)
SETTLED = HoldState(ask=AskPhase.SETTLED, asked_at=90.0)
REFUSED = HoldState(ask=AskPhase.REFUSED, asked_at=90.0, answer=AnswerWindow(retry_left=True))
RESET = HoldState()
WAIT = ProfileDecision(action="wait")
WAIT_END = ProfileDecision(action="wait", emit_turn_boundary=True)
ASK = ProfileDecision(action="wait", nudge=True)
ASK_END = ProfileDecision(action="wait", emit_turn_boundary=True, nudge=True)
EXPIRY_ASK = ProfileDecision(action="wait", nudge=True, reset_deadline=True)
PUBLISH = ProfileDecision(action="complete", outcome=GOOD)
CUT_OFF = ProfileDecision(
    action="cleanup", outcome=CLOSED, cleanup_reason="transport_closed_during_turn"
)


def facts(**overrides: Any) -> HoldFacts:
    defaults: dict[str, Any] = {
        "now": 100.0,
        "end": None,
        "active_turn": False,
        "expired": False,
        "candidate": GOOD,
        "assessment": READY,
        "rearm": False,
        "dead": False,
        "accepts_signals": True,
        "skips_ask": False,
        "unread": (),
        "grace_seconds": 5.0,
        "timeout": TIMEOUT,
    }
    return HoldFacts(**(defaults | overrides))


# row, state before, facts, decision, state after (None: unchanged)
ROWS: list[tuple[str, HoldState, dict[str, Any], ProfileDecision, HoldState | None]] = [
    (
        "A1",
        SETTLED,
        {"end": TurnEnd(BAD, DrainAction(terminate=False, emit_turn_boundary=True))},
        ProfileDecision(action="clear", emit_turn_boundary=True),
        RESET,
    ),
    ("A2", SENT, {"end": CLOSED_END}, PUBLISH, None),
    # Past the deadline a turn cut off by the close is an ordinary timeout.
    ("A3", SENT, {"end": CLOSED_END, "active_turn": True, "expired": True}, TIMEOUT, RESET),
    (
        "A3",
        SENT,
        {"end": CLOSED_END, "assessment": BLOCKED, "expired": True},
        TIMEOUT,
        RESET,
    ),
    # A turn cut off by the close outdates the candidate, whatever the evidence says.
    ("A3b", SENT, {"end": CLOSED_END, "active_turn": True}, CUT_OFF, RESET),
    (
        "A3b",
        SENT,
        {"end": CLOSED_END, "active_turn": True, "assessment": BLOCKED},
        CUT_OFF,
        RESET,
    ),
    (
        "A3b",
        SENT,
        {"end": CLOSED_END, "active_turn": True, "assessment": UNKNOWN},
        CUT_OFF,
        RESET,
    ),
    (
        "A4",
        SENT,
        {"end": CLOSED_END, "assessment": UNKNOWN},
        WAIT_END,
        replace(SENT, ask=AskPhase.SETTLED),
    ),
    ("A4", RESET, {"end": CLOSED_END, "assessment": BLOCKED}, WAIT_END, RESET),
    (
        "A5",
        SETTLED,
        {"end": TurnEnd(BAD, TERMINATE)},
        ProfileDecision(action="fail", outcome=BAD),
        RESET,
    ),
    (
        "A5",
        SETTLED,
        {
            "end": CLOSED_END,
            "assessment": WorkAssessment(disposition="blocked", blockers=(TASK,), generation=0),
        },
        ProfileDecision(action="fail", outcome=CLOSED),
        RESET,
    ),
    ("B1", replace(SENT, answer=SPENT), {"dead": True}, TIMEOUT, RESET),
    # Delta 1: B1 sits above B2 and above a turn end's publish.
    ("B1", replace(SETTLED, answer=SPENT), {"end": GOOD_END, "dead": True}, TIMEOUT, RESET),
    (
        "B1",
        replace(SETTLED, answer=SPENT),
        {"end": GOOD_END, "dead": True, "rearm": True},
        TIMEOUT,
        RESET,
    ),
    (
        "B2",
        SETTLED,
        {"rearm": True},
        ProfileDecision(action="abandon_candidate", reset_deadline=True),
        HoldState(asked_at=90.0, rearmed=True, stays=True, lease=SpacedLease(360.0)),
    ),
    (
        "B2",
        SETTLED,
        {"rearm": True, "end": GOOD_END, "assessment": BLOCKED},
        ProfileDecision(action="abandon_candidate", emit_turn_boundary=True, reset_deadline=True),
        HoldState(asked_at=90.0, rearmed=True, stays=True, lease=HeldLease(frozenset({CHILD}))),
    ),
    (
        "B2",
        RESET,
        {"rearm": True},
        ProfileDecision(action="abandon_candidate", reset_deadline=True),
        HoldState(rearmed=True, stays=True),
    ),
    (
        "C1",
        HoldState(rearmed=True),
        {"candidate": None, "expired": True},
        EXPIRY_ASK,
        HoldState(rearmed=True, answer=OPEN),
    ),
    ("C2", HoldState(rearmed=True), {"candidate": None}, WAIT, None),
    (
        "D1",
        HoldState(closing=True),
        {"assessment": UNKNOWN, "expired": True},
        ProfileDecision(action="fail", outcome=UNREADABLE),
        RESET,
    ),
    ("D2", HoldState(closing=True), {"assessment": UNKNOWN}, WAIT, None),
    (
        "D2",
        HoldState(closing=True),
        {"assessment": UNKNOWN, "expired": True, "end": GOOD_END},
        WAIT_END,
        None,
    ),
    ("D3", HoldState(closing=True), {}, PUBLISH, None),
    # A wedged `done` is asked, not published.
    (
        "E1",
        HoldState(closing=True),
        {"active_turn": True, "expired": True},
        EXPIRY_ASK,
        HoldState(closing=True, answer=OPEN),
    ),
    ("E1", SETTLED, {"expired": True}, EXPIRY_ASK, replace(SETTLED, answer=OPEN)),
    (
        "E1",
        replace(SETTLED, answer=OPEN),
        {"expired": True},
        EXPIRY_ASK,
        replace(SETTLED, answer=SPENT),
    ),
    ("E1", replace(SETTLED, answer=SPENT), {"expired": True}, TIMEOUT, RESET),
    ("E1", SETTLED, {"expired": True, "dead": True}, TIMEOUT, RESET),
    ("E1", SETTLED, {"expired": True, "accepts_signals": False}, TIMEOUT, RESET),
    ("E2", HoldState(closing=True), {"active_turn": True}, WAIT, None),
    ("E3", RESET, {"assessment": UNKNOWN}, WAIT, None),
    (
        "E3",
        HoldState(rearmed=True),
        {"assessment": UNKNOWN, "end": GOOD_END},
        WAIT_END,
        RESET,
    ),
    ("E4", RESET, {}, ASK, None),
    ("E4", RESET, {"end": GOOD_END}, ASK_END, None),
    # A refused expiry ask gets one more ask at a turn end, none between them.
    ("E4", REFUSED, {"end": GOOD_END}, ASK_END, None),
    (
        "E4",
        HoldState(lease=HeldLease(frozenset({TASK}))),
        {"assessment": BLOCKED},
        ASK,
        RESET,
    ),
    # `decide` retires a lease at its time, so a past-due one cannot stay a wake.
    # Blockers cleared: the lease is over.
    ("E4", HoldState(lease=HeldLease(frozenset({CHILD}))), {}, ASK, RESET),
    ("E4", HoldState(lease=SpacedLease(100.0)), {}, ASK, RESET),
    ("E6", replace(SENT, lease=SpacedLease(100.0)), {}, WAIT, SENT),
    ("E5", SETTLED, {}, PUBLISH, None),
    ("E5", SETTLED, {"end": GOOD_END}, PUBLISH, None),
    ("E5", RESET, {"accepts_signals": False}, PUBLISH, None),
    ("E5", REFUSED, {}, PUBLISH, None),
    # The grace ends at decide, so no timer goes past due.
    (
        "E5",
        replace(SENT, asked_at=95.0),
        {},
        PUBLISH,
        replace(SENT, ask=AskPhase.SETTLED, asked_at=95.0),
    ),
    ("E6", SENT, {}, WAIT, None),
    # Delta 3: a turn end inside the grace waits and emits a boundary.
    ("E6", SENT, {"end": GOOD_END}, WAIT_END, None),
    ("E6", SETTLED, {"assessment": BLOCKED}, WAIT, None),
    ("E6", HoldState(rearmed=True), {}, WAIT, None),
    (
        "E6",
        HoldState(lease=HeldLease(frozenset({CHILD}))),
        {"assessment": BLOCKED},
        WAIT,
        None,
    ),
    (
        "E6",
        HoldState(lease=HeldLease(frozenset({CHILD}))),
        {
            "assessment": WorkAssessment(
                disposition="blocked", blockers=(CHILD, FOLLOWUP), generation=0
            )
        },
        WAIT,
        None,
    ),
    ("E6", HoldState(lease=SpacedLease(101.0)), {}, WAIT, None),
    # A harness that reports its work: nothing runs closes, blockers hold, neither asks.
    ("E5", RESET, {"skips_ask": True}, PUBLISH, None),
    ("E5", RESET, {"skips_ask": True, "end": GOOD_END}, PUBLISH, None),
    ("E6", RESET, {"skips_ask": True, "end": GOOD_END, "assessment": BLOCKED}, WAIT_END, None),
    ("E3", RESET, {"skips_ask": True, "end": GOOD_END, "assessment": UNKNOWN}, WAIT_END, None),
    # An ask still out there is answered first.
    ("E6", SENT, {"skips_ask": True}, WAIT, None),
    # A rearm stands until an ask ends it, on every harness.
    ("E4", HoldState(stays=True), {"skips_ask": True}, ASK, HoldState(stays=True)),
    (
        "E4",
        HoldState(stays=True, lease=HeldLease(frozenset({CHILD}))),
        {"skips_ask": True},
        ASK,
        HoldState(stays=True),
    ),
    (
        "E6",
        HoldState(stays=True, lease=HeldLease(frozenset({CHILD}))),
        {"skips_ask": True, "assessment": BLOCKED},
        WAIT,
        None,
    ),
    ("E5", HoldState(stays=True, ask=AskPhase.SETTLED), {"skips_ask": True}, PUBLISH, None),
    (
        "B2",
        HoldState(woken=frozenset({"p2:1"})),
        {"rearm": True},
        ProfileDecision(action="abandon_candidate", reset_deadline=True),
        HoldState(rearmed=True, stays=True, woken=frozenset({"p2:1"})),
    ),
]


@pytest.mark.parametrize(("row", "before", "overrides", "decision", "after"), ROWS)
def test_row(
    row: str,
    before: HoldState,
    overrides: dict[str, Any],
    decision: ProfileDecision,
    after: HoldState | None,
) -> None:
    step = decide(before, facts(**overrides))
    assert step.decision == decision
    assert step.state == (before if after is None else after)
    assert step.leave_awaiting is (row in {"D2", "E2", "E3"} and "end" not in overrides)
    assert step.wake == ()


# state before, facts, children named, state after. Only a ready tree with no turn running
# wakes, because anything else still has a turn to come.
WOKEN_FIRST = HoldState(woken=frozenset({"p2:1"}))
WAKES_PARENT: list[tuple[HoldState, dict[str, Any], tuple[FinishedChild, ...], HoldState]] = [
    (RESET, {"unread": (FIRST,)}, (FIRST,), WOKEN_FIRST),
    # On both kinds of harness, and with no candidate after a rearm.
    (RESET, {"unread": (FIRST,), "skips_ask": True}, (FIRST,), WOKEN_FIRST),
    (
        HoldState(rearmed=True),
        {"unread": (FIRST,), "candidate": None},
        (FIRST,),
        HoldState(rearmed=True, woken=frozenset({"p2:1"})),
    ),
    # A rearm's quiet covers a turn end, not what the child has not read.
    (
        HoldState(lease=SpacedLease(200.0)),
        {"unread": (FIRST,)},
        (FIRST,),
        HoldState(lease=SpacedLease(200.0), woken=frozenset({"p2:1"})),
    ),
    # Once each: a second child gets its own wake, naming only it.
    (
        WOKEN_FIRST,
        {"unread": (FIRST, SECOND)},
        (SECOND,),
        HoldState(woken=frozenset({"p2:1", "p3:1"})),
    ),
]


@pytest.mark.parametrize(("before", "overrides", "wake", "after"), WAKES_PARENT)
def test_wake_parent(
    before: HoldState,
    overrides: dict[str, Any],
    wake: tuple[FinishedChild, ...],
    after: HoldState,
) -> None:
    step = decide(before, facts(**overrides))
    assert step.decision == ASK
    assert step.wake == wake
    assert step.state == after


# No wake: the row falls through to what the same state does without an owed child.
QUIET_PARENT: list[tuple[HoldState, dict[str, Any], ProfileDecision]] = [
    (RESET, {"assessment": BLOCKED}, ASK),  # a partial clear stays silent
    (RESET, {"assessment": BLOCKED, "skips_ask": True}, WAIT),
    (RESET, {"active_turn": True}, WAIT),
    (RESET, {"assessment": UNKNOWN}, WAIT),
    (SENT, {}, WAIT),
    (RESET, {"accepts_signals": False}, PUBLISH),
    (HoldState(closing=True), {}, PUBLISH),  # `done` is the child's own word
    (WOKEN_FIRST, {"skips_ask": True}, PUBLISH),  # woken and said nothing more
    (WOKEN_FIRST, {}, ASK),
]


@pytest.mark.parametrize(("before", "overrides", "decision"), QUIET_PARENT)
def test_no_wake(before: HoldState, overrides: dict[str, Any], decision: ProfileDecision) -> None:
    step = decide(before, facts(unread=(FIRST,), **overrides))
    assert step.wake == ()
    assert step.decision == decision


# kind, before the send (as `decide` left it), result, after; every attempt stamps `asked_at`
EXPIRY = HoldState(answer=OPEN)
REFUSED_EXPIRY = HoldState(ask=AskPhase.REFUSED, answer=OPEN)
SENDS: list[tuple[str, HoldState, SendResult, HoldState]] = [
    ("expiry delivered", EXPIRY, SendResult.SENT, HoldState(ask=AskPhase.SENT, answer=SPENT)),
    (
        "expiry retry delivered",
        replace(REFUSED_EXPIRY, answer=SPENT),
        SendResult.SENT,
        HoldState(ask=AskPhase.SENT, answer=SPENT),
    ),
    ("turn end delivered", RESET, SendResult.SENT, HoldState(ask=AskPhase.SENT)),
    ("expiry refused, unasked", EXPIRY, SendResult.REFUSED, REFUSED_EXPIRY),
    ("expiry refused again", REFUSED_EXPIRY, SendResult.REFUSED, REFUSED_EXPIRY),
    (
        "expiry refused, asked before",
        replace(SETTLED, answer=OPEN),
        SendResult.REFUSED,
        replace(SETTLED, answer=OPEN),
    ),
    ("turn end refused", RESET, SendResult.REFUSED, HoldState(ask=AskPhase.SETTLED)),
    ("expiry dead", EXPIRY, SendResult.DEAD, HoldState(ask=AskPhase.SETTLED, answer=SPENT)),
    ("turn end dead", RESET, SendResult.DEAD, HoldState(ask=AskPhase.SETTLED)),
]


@pytest.mark.parametrize(("kind", "before", "result", "after"), SENDS)
def test_send(kind: str, before: HoldState, result: SendResult, after: HoldState) -> None:
    del kind
    assert after_send(before, result, now=100.0) == replace(after, asked_at=100.0)


def test_grace_starts_when_the_send_ends() -> None:
    folded = after_send(RESET, SendResult.SENT, now=100.0)
    assert folded.asked_at == 100.0
    assert decide(folded, facts()).decision == WAIT


def completion(**overrides: Any) -> CompletionState:
    defaults: dict[str, Any] = {
        "phase": "waiting",
        "candidate": GOOD,
        "assessment": READY,
        "deadline_at": None,
        "stabilization_at": None,
        "stabilization_generation": None,
    }
    return CompletionState(**(defaults | overrides))


TASK_BLOCKED = WorkAssessment(disposition="blocked", blockers=(TASK,), generation=0)
STREAM_CLOSED = TerminalEventOutcome(
    status=SpawnStatus.FAILED, exit_code=1, error="stream_closed_while_awaiting_done"
)
# coordinator state, intentional stop, dead, outcome
CLOSES: list[tuple[dict[str, Any], bool, bool, TerminalEventOutcome | None]] = [
    ({"candidate": None}, False, False, None),
    ({"candidate": None}, True, True, None),
    # An idle child with a ready report, even when cancelled or dead.
    ({}, False, False, GOOD),
    ({}, True, False, GOOD),
    ({}, False, True, GOOD),
    # Never over a running turn.
    ({"active_turn": True}, True, False, STREAM_CLOSED),
    ({"active_turn": True}, False, False, STREAM_CLOSED),
    ({"active_turn": True}, False, True, BACKEND_DEAD_OUTCOME),
    ({"active_turn": True}, True, True, STREAM_CLOSED),
    ({"assessment": BLOCKED}, False, False, STREAM_CLOSED),
    ({"assessment": BLOCKED}, True, True, STREAM_CLOSED),
    ({"assessment": BLOCKED}, False, True, BACKEND_DEAD_OUTCOME),
    ({"assessment": None}, False, False, STREAM_CLOSED),
]


@pytest.mark.parametrize(("state", "intentional", "dead", "outcome"), CLOSES)
def test_close(
    state: dict[str, Any], intentional: bool, dead: bool, outcome: TerminalEventOutcome | None
) -> None:
    closed = outcome_at_close(completion(**state), intentional_stop=intentional, dead=dead)
    assert closed == outcome


# hold state, coordinator state, dead, defers
DEFERS: list[tuple[HoldState, dict[str, Any], bool, bool]] = [
    (RESET, {}, False, False),
    (RESET, {"assessment": BLOCKED}, False, True),
    (RESET, {"assessment": BLOCKED}, True, False),
    (RESET, {"assessment": TASK_BLOCKED}, False, False),
    (RESET, {"assessment": BLOCKED, "candidate": None}, False, False),
    (RESET, {"assessment": None}, False, False),
    (HoldState(rearmed=True), {}, False, True),
    (HoldState(rearmed=True), {"candidate": None}, False, True),
    (HoldState(rearmed=True), {}, True, False),
]


@pytest.mark.parametrize(("hold", "state", "dead", "defers"), DEFERS)
def test_defer_close(hold: HoldState, state: dict[str, Any], dead: bool, defers: bool) -> None:
    assert defers_close(hold, completion(**state), dead=dead) is defers


# state, done, rearm, state after, rearm stands
CLOSING = HoldState(closing=True)
SIGNALS: list[tuple[HoldState, bool, bool, HoldState, bool]] = [
    (RESET, False, False, RESET, False),
    (RESET, True, False, CLOSING, False),
    (CLOSING, False, False, CLOSING, False),
    (RESET, False, True, RESET, True),
    (RESET, True, True, RESET, True),
    (CLOSING, False, True, RESET, True),
    # A `done` after a rearm: the state a rearm leaves still takes it.
    (HoldState(rearmed=True), True, False, HoldState(rearmed=True, closing=True), False),
    (SENT, True, False, replace(SENT, closing=True), False),
    (SETTLED, False, True, SETTLED, True),
]


@pytest.mark.parametrize(("before", "done", "rearm", "after", "stands"), SIGNALS)
def test_signals(
    before: HoldState, done: bool, rearm: bool, after: HoldState, stands: bool
) -> None:
    assert after_signals(before, done=done, rearm=rearm) == (after, stands)


# state, has a candidate, wake. The grace is 5 s.
WAKES: list[tuple[HoldState, bool, float | None]] = [
    (RESET, True, None),
    (SETTLED, True, None),
    (SENT, True, 104.0),
    (SENT, False, 104.0),
    (HoldState(lease=SpacedLease(200.0)), True, 200.0),
    (HoldState(lease=SpacedLease(200.0)), False, None),
    (replace(SENT, lease=SpacedLease(101.0)), True, 101.0),
    (replace(SENT, lease=SpacedLease(200.0)), True, 104.0),
    (HoldState(lease=HeldLease(frozenset({CHILD}))), True, None),
]


@pytest.mark.parametrize(("state", "has_candidate", "wake"), WAKES)
def test_wake(state: HoldState, has_candidate: bool, wake: float | None) -> None:
    assert next_wake(state, 5.0, has_candidate=has_candidate) == wake


# state, action, deadline: 300 s after now while `answer` is open, otherwise the window
DEADLINES: list[tuple[HoldState, ProfileAction, float | None]] = [
    (RESET, "wait", 900.0),
    (RESET, "complete", 900.0),
    (RESET, "abandon_candidate", 900.0),
    (HoldState(answer=OPEN), "wait", 400.0),
    (HoldState(answer=OPEN), "fail", None),
    (RESET, "cleanup", None),
    (RESET, "clear", None),
]


@pytest.mark.parametrize(("state", "action", "deadline"), DEADLINES)
def test_deadline(state: HoldState, action: ProfileAction, deadline: float | None) -> None:
    assert next_deadline(state, action, now=100.0, window_ends=900.0) == deadline


def test_events() -> None:
    held = replace(SENT, answer=SPENT)
    assert after_event(held, turn_started=True, work_in_turn=False) == replace(
        held, ask=AskPhase.SETTLED
    )
    assert after_event(held, turn_started=False, work_in_turn=True) == replace(held, answer=None)
    assert after_event(RESET, turn_started=True, work_in_turn=False) == RESET
    assert after_event(REFUSED, turn_started=True, work_in_turn=False) == REFUSED
    # Work closes the window: the child is due a fresh turn-end ask.
    assert after_event(REFUSED, turn_started=False, work_in_turn=True) == replace(
        REFUSED, ask=AskPhase.UNASKED, answer=None
    )
