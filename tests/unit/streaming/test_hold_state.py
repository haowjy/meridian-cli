"""The hold's precedence table: one case per row and per send outcome."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.harness.semantics import TerminalEventOutcome, TerminalOutcomeCause
from meridian.lib.streaming.completion_contracts import (
    DiagnosticBlocker,
    EvidenceFailure,
    ProfileDecision,
    WorkAssessment,
)
from meridian.lib.streaming.drain_policy import DrainAction
from meridian.lib.streaming.hold_state import (
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
    decide,
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
TERMINATE = DrainAction(terminate=True, emit_turn_boundary=False)
GOOD_END = TurnEnd(GOOD, TERMINATE)
CLOSED_END = TurnEnd(CLOSED, TERMINATE)
OPEN = AnswerWindow(retry_left=True)
SPENT = AnswerWindow(retry_left=False)
SENT = HoldState(ask=AskPhase.SENT, asked_at=99.0)
SETTLED = HoldState(ask=AskPhase.SETTLED, asked_at=90.0)
RESET = HoldState()
WAIT = ProfileDecision(action="wait")
WAIT_END = ProfileDecision(action="wait", emit_turn_boundary=True)
ASK = ProfileDecision(action="wait", nudge=True)
ASK_END = ProfileDecision(action="wait", emit_turn_boundary=True, nudge=True)
EXPIRY_ASK = ProfileDecision(action="wait", nudge=True, reset_deadline=True)
PUBLISH = ProfileDecision(action="complete", outcome=GOOD)


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
        "grace_seconds": 5.0,
        "timeout": TIMEOUT,
    }
    return HoldFacts(**(defaults | overrides))


# row, state before, facts, decision, state after (None: unchanged), enter_wait
ROWS: list[tuple[str, HoldState, dict[str, Any], ProfileDecision, HoldState | None, bool]] = [
    (
        "A1",
        SETTLED,
        {"end": TurnEnd(BAD, DrainAction(terminate=False, emit_turn_boundary=True))},
        ProfileDecision(action="clear", emit_turn_boundary=True),
        RESET,
        False,
    ),
    ("A2", SENT, {"end": CLOSED_END}, PUBLISH, None, True),
    (
        "A3",
        SENT,
        {"end": CLOSED_END, "assessment": BLOCKED, "expired": True},
        TIMEOUT,
        RESET,
        False,
    ),
    (
        "A4",
        SENT,
        {"end": CLOSED_END, "assessment": UNKNOWN},
        WAIT_END,
        replace(SENT, ask=AskPhase.SETTLED),
        True,
    ),
    ("A4", RESET, {"end": CLOSED_END, "assessment": BLOCKED}, WAIT_END, RESET, True),
    (
        "A5",
        SETTLED,
        {"end": TurnEnd(BAD, TERMINATE)},
        ProfileDecision(action="fail", outcome=BAD),
        RESET,
        False,
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
        False,
    ),
    ("B1", replace(SENT, answer=SPENT), {"dead": True}, TIMEOUT, RESET, False),
    # Delta 1: B1 sits above B2 and above a turn end's publish.
    ("B1", replace(SETTLED, answer=SPENT), {"end": GOOD_END, "dead": True}, TIMEOUT, RESET, False),
    (
        "B1",
        replace(SETTLED, answer=SPENT),
        {"end": GOOD_END, "dead": True, "rearm": True},
        TIMEOUT,
        RESET,
        False,
    ),
    (
        "B2",
        SETTLED,
        {"rearm": True},
        ProfileDecision(action="abandon_candidate", reset_deadline=True),
        HoldState(asked_at=90.0, rearmed=True, lease=SpacedLease(360.0)),
        True,
    ),
    (
        "B2",
        SETTLED,
        {"rearm": True, "end": GOOD_END, "assessment": BLOCKED},
        ProfileDecision(action="abandon_candidate", emit_turn_boundary=True, reset_deadline=True),
        HoldState(asked_at=90.0, rearmed=True, lease=HeldLease(frozenset({CHILD}))),
        True,
    ),
    (
        "B2",
        RESET,
        {"rearm": True},
        ProfileDecision(action="abandon_candidate", reset_deadline=True),
        HoldState(rearmed=True),
        True,
    ),
    (
        "C1",
        HoldState(rearmed=True),
        {"candidate": None, "expired": True},
        EXPIRY_ASK,
        HoldState(rearmed=True, answer=OPEN),
        True,
    ),
    ("C2", HoldState(rearmed=True), {"candidate": None}, WAIT, None, True),
    (
        "D1",
        HoldState(closing=True),
        {"assessment": UNKNOWN, "expired": True},
        ProfileDecision(action="fail", outcome=UNREADABLE),
        RESET,
        False,
    ),
    ("D2", HoldState(closing=True), {"assessment": UNKNOWN}, WAIT, None, False),
    (
        "D2",
        HoldState(closing=True),
        {"assessment": UNKNOWN, "expired": True, "end": GOOD_END},
        WAIT_END,
        None,
        True,
    ),
    ("D3", HoldState(closing=True), {}, PUBLISH, None, True),
    # A wedged `done` is asked, not published.
    (
        "E1",
        HoldState(closing=True),
        {"active_turn": True, "expired": True},
        EXPIRY_ASK,
        HoldState(closing=True, answer=OPEN),
        True,
    ),
    ("E1", SETTLED, {"expired": True}, EXPIRY_ASK, replace(SETTLED, answer=OPEN), True),
    (
        "E1",
        replace(SETTLED, answer=OPEN),
        {"expired": True},
        EXPIRY_ASK,
        replace(SETTLED, answer=SPENT),
        True,
    ),
    ("E1", replace(SETTLED, answer=SPENT), {"expired": True}, TIMEOUT, RESET, False),
    ("E1", SETTLED, {"expired": True, "dead": True}, TIMEOUT, RESET, False),
    ("E1", SETTLED, {"expired": True, "accepts_signals": False}, TIMEOUT, RESET, False),
    ("E2", HoldState(closing=True), {"active_turn": True}, WAIT, None, False),
    ("E3", RESET, {"assessment": UNKNOWN}, WAIT, None, False),
    (
        "E3",
        HoldState(rearmed=True),
        {"assessment": UNKNOWN, "end": GOOD_END},
        WAIT_END,
        RESET,
        True,
    ),
    ("E4", RESET, {}, ASK, None, True),
    ("E4", RESET, {"end": GOOD_END}, ASK_END, None, True),
    # A refused expiry ask gets one more ask at a turn end, none between them.
    ("E4", HoldState(answer=OPEN), {"end": GOOD_END}, ASK_END, None, True),
    (
        "E4",
        HoldState(lease=HeldLease(frozenset({TASK}))),
        {"assessment": BLOCKED},
        ASK,
        RESET,
        True,
    ),
    (
        "E4",
        HoldState(lease=SpacedLease(100.0), rearmed=False),
        {},
        ASK,
        None,
        True,
    ),
    ("E5", SETTLED, {}, PUBLISH, None, True),
    ("E5", SETTLED, {"end": GOOD_END}, PUBLISH, None, True),
    ("E5", RESET, {"accepts_signals": False}, PUBLISH, None, True),
    ("E5", HoldState(answer=OPEN), {}, PUBLISH, None, True),
    # The grace ends at decide, so no timer goes past due.
    (
        "E5",
        replace(SENT, asked_at=95.0),
        {},
        PUBLISH,
        replace(SENT, ask=AskPhase.SETTLED, asked_at=95.0),
        True,
    ),
    ("E6", SENT, {}, WAIT, None, True),
    # Delta 3: a turn end inside the grace waits and emits a boundary.
    ("E6", SENT, {"end": GOOD_END}, WAIT_END, None, True),
    ("E6", SETTLED, {"assessment": BLOCKED}, WAIT, None, True),
    ("E6", HoldState(rearmed=True), {}, WAIT, None, True),
    (
        "E6",
        HoldState(lease=HeldLease(frozenset({CHILD}))),
        {"assessment": BLOCKED},
        WAIT,
        None,
        True,
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
        True,
    ),
    ("E6", HoldState(lease=SpacedLease(101.0)), {}, WAIT, None, True),
]


@pytest.mark.parametrize(("row", "before", "overrides", "decision", "after", "enter"), ROWS)
def test_row(
    row: str,
    before: HoldState,
    overrides: dict[str, Any],
    decision: ProfileDecision,
    after: HoldState | None,
    enter: bool,
) -> None:
    step = decide(before, facts(**overrides))
    assert step.row == row
    assert step.decision == decision
    assert step.state == (before if after is None else after)
    assert step.enter_wait is enter


# kind, before the send (ask still as it was, `answer` as `decide` left it), result, after
EXPIRY = HoldState(answer=OPEN)
SENDS: list[tuple[str, HoldState, SendResult, HoldState]] = [
    (
        "expiry delivered",
        EXPIRY,
        SendResult.SENT,
        HoldState(ask=AskPhase.SENT, asked_at=100.0, answer=SPENT),
    ),
    (
        "turn end delivered",
        RESET,
        SendResult.SENT,
        HoldState(ask=AskPhase.SENT, asked_at=100.0),
    ),
    ("expiry refused, unasked", EXPIRY, SendResult.REFUSED, replace(EXPIRY, ask=AskPhase.UNASKED)),
    (
        "expiry refused, asked before",
        replace(SETTLED, answer=OPEN),
        SendResult.REFUSED,
        replace(SETTLED, answer=OPEN),
    ),
    ("turn end refused", RESET, SendResult.REFUSED, HoldState(ask=AskPhase.SETTLED)),
    (
        "expiry dead",
        EXPIRY,
        SendResult.DEAD,
        HoldState(ask=AskPhase.SETTLED, answer=SPENT),
    ),
    ("turn end dead", RESET, SendResult.DEAD, HoldState(ask=AskPhase.SETTLED)),
]


@pytest.mark.parametrize(("kind", "before", "result", "after"), SENDS)
def test_send(kind: str, before: HoldState, result: SendResult, after: HoldState) -> None:
    del kind
    assert after_send(before, result, prior=before.ask, now=100.0) == after


def test_grace_starts_when_the_send_ends() -> None:
    # send_nudge stamps `asked_at` before the await; the fold moves it.
    started = replace(SENT, asked_at=67.0)
    folded = after_send(started, SendResult.SENT, prior=AskPhase.UNASKED, now=100.0)
    assert folded.asked_at == 100.0
    assert decide(folded, facts()).row == "E6"


def test_events() -> None:
    held = replace(SENT, answer=SPENT)
    assert after_event(held, turn_started=True, work_in_turn=False) == replace(
        held, ask=AskPhase.SETTLED
    )
    assert after_event(held, turn_started=False, work_in_turn=True) == replace(held, answer=None)
    assert after_event(RESET, turn_started=True, work_in_turn=False) == RESET
