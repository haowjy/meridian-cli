"""The resident hold's lifecycle: one state value, one precedence table and the folds.

No I/O, no async, no clock. `_HoldProfile` in `resident_drain.py` reads the files and the
clock, sends, and applies what these functions return. Why the rows are as they are:
`.context/completion-and-entry-points.md`. `decide` marks each row's name.
"""

from __future__ import annotations

from collections.abc import Sequence
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
NATIVE_WAKE_SETTLE_SECONDS = 1.0  # how long a queued native wake gets to start its turn
_TURN_CUT_OFF = "transport_closed_during_turn"
_CLOSED_BY_CHILD = "closed_by_child"
_REPLY_FAILED = "reply_failed"
_STREAM_CLOSED = TerminalEventOutcome(
    status=SpawnStatus.FAILED, exit_code=1, error="stream_closed_while_awaiting_done"
)
BACKEND_DEAD_OUTCOME = TerminalEventOutcome(
    status=SpawnStatus.FAILED, exit_code=1, error="backend_dead_while_awaiting_done"
)


class AskPhase(StrEnum):
    UNASKED = "unasked"  # nothing asked since the last reset
    REFUSED = "refused"  # an expiry ask or a wake was refused, so nothing reached the child
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
    """Rearm with nothing visible: the next ask waits for this time, from the last turn end."""

    not_before: float


@dataclass(frozen=True)
class WindowLease:
    """`rearm --long` with nothing visible: the next ask is the idle limit's."""


Lease = HeldLease | SpacedLease | WindowLease


@dataclass(frozen=True)
class HoldState:
    """ "Reset" means `HoldState()`."""

    ask: AskPhase = AskPhase.UNASKED
    asked_at: float | None = None  # when the last send attempt ended
    answer: AnswerWindow | None = None
    lease: Lease | None = None
    rearmed: bool = False  # the next good turn end is the next candidate
    closing: bool = False  # `done` latched
    stays: bool = False  # a rearm stands: only an ask can end it, even where turn ends skip one
    woken: frozenset[str] = frozenset()  # owed launches a delivered ask or wake has named
    reply_replaces: bool = False  # a wake or an ask between turns: the answer is the report
    reply_closes: bool = False  # an expiry ask landed between turns: the next turn end closes


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
    rearm: bool  # signalled and within budget
    rearm_long: bool  # that rearm asked for the idle limit, not the spacing
    dead: bool  # connection health says BACKEND_DEAD
    accepts_signals: bool
    skips_ask: bool  # the harness reports its own work, so a turn end needs no confirmation
    unread: tuple[FinishedChild, ...]  # finished children the parent has not consumed
    wake_coming: bool  # a hold cleared at this turn end and the settle has not run out
    grace_seconds: float
    timeout: ProfileDecision  # RearmWindow.timeout_decision(), so the budget error stays there


@dataclass(frozen=True)
class HoldStep:
    state: HoldState
    decision: ProfileDecision
    leave_awaiting: bool = False  # non-turn wakes can leave awaiting state unchanged
    wake: tuple[FinishedChild, ...] = ()  # set on a nudge: the finished children it must name


def decide(state: HoldState, facts: HoldFacts) -> HoldStep:
    # Timers past due end here, so neither can stay a wake.
    if state.ask is AskPhase.SENT and facts.now >= _grace_ends(state, facts.grace_seconds):
        state = replace(state, ask=AskPhase.SETTLED)
    state = replace(state, lease=_run_spacing(state.lease, facts))
    end = facts.end
    disposition = facts.assessment.disposition
    if end is not None:
        state = replace(state, reply_replaces=False)

    if end is not None and not end.action.terminate:
        decision = ProfileDecision(action="clear", emit_turn_boundary=end.action.emit_turn_boundary)
        return HoldStep(HoldState(), decision)
    if end is not None and end.outcome.status != "succeeded":
        if (
            facts.candidate is not None
            and end.outcome.cause is TerminalOutcomeCause.REPLACEABLE_TRANSPORT_CLOSE
        ):
            if disposition == "ready" and not facts.active_turn:
                return _publish(state, facts)
            if facts.expired:
                return _timeout(facts)
            if facts.active_turn:
                # The turn outdated the candidate, and nothing that follows can vouch for
                # it, so no later row may publish: end here, without orphaning descendants.
                decision = ProfileDecision(
                    action="cleanup", outcome=end.outcome, cleanup_reason=_TURN_CUT_OFF
                )
                return HoldStep(HoldState(), decision)
            if disposition == "unknown" or _descendant_blocked(facts.assessment):
                ask = AskPhase.SETTLED if state.ask is AskPhase.SENT else state.ask
                return _wait(replace(state, ask=ask), facts)
            if state.closing:
                return _close(facts)
        if state.reply_closes:
            # The failure stands and the report stays, but this was a close: the ask said so.
            decision = ProfileDecision(
                action="cleanup", outcome=end.outcome, cleanup_reason=_REPLY_FAILED
            )
            return HoldStep(HoldState(), decision)
        decision = ProfileDecision(action="fail", outcome=end.outcome)
        return HoldStep(HoldState(), decision)
    if state.answer is not None and facts.dead:
        return _timeout(facts)
    if facts.rearm:
        decision = ProfileDecision(
            action="abandon_candidate", emit_turn_boundary=end is not None, reset_deadline=True
        )
        return HoldStep(_after_rearm(state, facts), decision)
    if facts.candidate is None:
        if facts.expired:
            return _expiry(state, facts)
        wake = _wake(state, facts)
        if wake is not None:
            return wake
        if _rearm_ran_out(state, facts):
            return _expiry(state, facts)
        return _wait(state, facts)
    if end is not None and state.reply_closes:
        return _close(facts)
    if state.closing:
        if disposition == "unknown":
            if facts.expired and end is None:
                decision = ProfileDecision(action="fail", outcome=_UNREADABLE)
                return HoldStep(HoldState(), decision)
            return _wait(state, facts, leave_awaiting=end is None)
        if not facts.active_turn:
            return _close(facts)
    if facts.expired:
        return _expiry(state, facts)
    if end is not None:
        state = replace(state, rearmed=False)
    if facts.active_turn:
        return _wait(state, facts, leave_awaiting=end is None)
    if disposition == "unknown":
        return _wait(state, facts, leave_awaiting=end is None)
    wake = _wake(state, facts)
    if wake is not None:
        return wake
    if (
        facts.accepts_signals
        and (state.stays or not facts.skips_ask)
        and (state.ask is AskPhase.UNASKED or (state.ask is AskPhase.REFUSED and end is not None))
        and not _lease_quiet(state.lease, facts.assessment)
    ):
        return _wait(replace(state, lease=None), facts, nudge=True)
    if disposition == "ready" and _settled(state, facts):
        if facts.wake_coming:
            decision = ProfileDecision(action="stabilize", emit_turn_boundary=end is not None)
            return HoldStep(state, decision)
        return _publish(state, facts)
    return _wait(state, facts)


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
    if action not in {"wait", "stabilize", "complete", "abandon_candidate"}:
        return None
    return window_ends if state.answer is None else now + IDLE_NUDGE_ANSWER_SECONDS


def next_wake(state: HoldState, grace_seconds: float, *, active_turn: bool) -> float | None:
    """When the table next changes its answer with no event: both timers end in `decide`."""
    due: list[float] = []
    if state.ask is AskPhase.SENT:
        due.append(_grace_ends(state, grace_seconds))
    if isinstance(state.lease, SpacedLease) and not active_turn:
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


def after_send(
    state: HoldState,
    result: SendResult,
    *,
    now: float,
    active_turn: bool,
    wake: tuple[FinishedChild, ...] = (),
) -> HoldState:
    """Fold one send attempt at the time it ended. An open `answer` means expiry kind."""
    state = replace(state, asked_at=now)
    if result is not SendResult.REFUSED:
        # A dead backend has nobody left to tell, and an unmarked wake would repeat at once.
        state = replace(state, woken=state.woken | {child.key for child in wake})
    if result is SendResult.SENT:
        if state.answer is None:
            return replace(state, ask=AskPhase.SENT)
        # Between turns only: a running turn's end is an ordinary turn end, as it was when
        # the turn started. An unknown result counts: if the ask never arrived, no turn comes.
        landed = not active_turn
        return replace(
            state,
            ask=AskPhase.SENT,
            answer=AnswerWindow(retry_left=False),
            reply_closes=state.reply_closes or landed,
            reply_replaces=state.reply_replaces or landed,
        )
    if result is SendResult.DEAD and state.answer is not None:
        state = replace(state, answer=AnswerWindow(retry_left=False))
    if result is SendResult.REFUSED and (
        wake or (state.answer is not None and state.ask in _NOTHING_ASKED)
    ):
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


def _close(facts: HoldFacts) -> HoldStep:
    """The child's own close, by `done` or a reply: whatever still runs goes with it."""
    assert facts.candidate is not None
    decision = ProfileDecision(
        action="cleanup", outcome=facts.candidate, cleanup_reason=_CLOSED_BY_CHILD
    )
    return HoldStep(HoldState(), decision)


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
        # A reply closes with blockers too, so this ask can't leave what is owed to a wake.
        wake = _owed(state, facts)
        held = replace(
            state,
            answer=AnswerWindow(retry_left=state.answer is None),
            reply_replaces=state.reply_replaces or bool(wake),
        )
        return HoldStep(held, decision, wake=wake)
    return _timeout(facts)


def _wake(state: HoldState, facts: HoldFacts) -> HoldStep | None:
    """Name what the parent is owed, once each, when nothing runs and no turn will come."""
    # A failed send re-evaluates at once, so a refusal waits for a turn end or the expiry ask.
    if (
        not facts.accepts_signals
        or facts.active_turn
        or state.ask is AskPhase.SENT
        or (state.ask is AskPhase.REFUSED and facts.end is None)
        or facts.assessment.disposition != "ready"
    ):
        return None
    wake = _owed(state, facts)
    if not wake:
        return None
    step = _wait(replace(state, reply_replaces=True), facts, nudge=True)
    return replace(step, wake=wake)


def _owed(state: HoldState, facts: HoldFacts) -> tuple[FinishedChild, ...]:
    return tuple(child for child in facts.unread if child.key not in state.woken)


def _settled(state: HoldState, facts: HoldFacts) -> bool:
    # A refused expiry ask: a ready tree may still publish, once nothing is left unnamed.
    return (
        not facts.accepts_signals
        or (facts.skips_ask and not state.stays and state.ask is AskPhase.UNASKED)
        or state.ask is AskPhase.SETTLED
        or (state.ask is AskPhase.REFUSED and not _owed(state, facts))
    )


def _grace_ends(state: HoldState, grace_seconds: float) -> float:
    assert state.asked_at is not None
    return state.asked_at + grace_seconds


def _lease_quiet(lease: Lease | None, assessment: WorkAssessment) -> bool:
    if isinstance(lease, HeldLease):
        blockers = assessment.blockers
        held = (b in lease.blockers or b.code == "awaiting_followup_result" for b in blockers)
        return bool(blockers) and all(held)
    return lease is not None  # `_run_spacing` ends a SpacedLease at `not_before`


def _run_spacing(lease: Lease | None, facts: HoldFacts) -> Lease | None:
    """Restart the spacing at a turn end, and end it only with no turn running."""
    if not isinstance(lease, SpacedLease):
        return lease
    if facts.end is not None:
        return SpacedLease(facts.now + COMPLETION_NUDGE_INTERVAL_SECONDS)
    if facts.active_turn or facts.now < lease.not_before:
        return lease
    # The spacing is for what cannot be seen; what can be seen ends the wait by itself.
    blockers = frozenset(facts.assessment.blockers)
    return HeldLease(blockers) if blockers else None


def _rearm_ran_out(state: HoldState, facts: HoldFacts) -> bool:
    """With no candidate nothing else can close, so this ask takes the expiry kind."""
    return (
        state.rearmed
        and state.ask is AskPhase.UNASKED
        and not facts.active_turn
        and facts.assessment.disposition != "unknown"
        and not _lease_quiet(state.lease, facts.assessment)
    )


def _after_rearm(state: HoldState, facts: HoldFacts) -> HoldState:
    blockers = frozenset(facts.assessment.blockers)
    lease: Lease
    if blockers:
        lease = HeldLease(blockers)
    elif facts.rearm_long:
        lease = WindowLease()
    else:
        lease = SpacedLease(facts.now + COMPLETION_NUDGE_INTERVAL_SECONDS)
    return HoldState(lease=lease, rearmed=True, stays=True, woken=state.woken)


def _descendant_blocked(assessment: WorkAssessment) -> bool:
    return assessment.disposition == "blocked" and any(
        blocker.source == "persisted_descendant" for blocker in assessment.blockers
    )


def blocker_labels(blockers: Sequence[DiagnosticBlocker]) -> tuple[str, ...]:
    return tuple(
        f"spawn {blocker.identity}"
        if blocker.source == "persisted_descendant"
        else blocker.identity or "a background task's follow-up result"
        for blocker in blockers
    )


def _running_lines(blockers: Sequence[DiagnosticBlocker]) -> tuple[list[str], list[str], bool]:
    descendants = [
        blocker.identity
        for blocker in blockers
        if blocker.source == "persisted_descendant" and blocker.identity is not None
    ]
    tasks = [
        blocker.identity
        for blocker in blockers
        if blocker.source == "native_task"
        and blocker.code == "running"
        and blocker.identity is not None
    ]
    awaiting = any(
        blocker.source == "native_task" and blocker.code == "awaiting_followup_result"
        for blocker in blockers
    )
    return descendants, tasks, awaiting


_REARM_FORMS = (
    '`meridian spawn rearm --reason "..."`. The next check comes 270 s after you go idle; '
    "add `--long` for a wait Meridian cannot see, such as remote CI, and it comes at the "
    "idle limit."
)


def _unread_line(wake: Sequence[FinishedChild]) -> str:
    finished = "; ".join(
        f"{child.spawn_id} ({child.status}), report at {child.report}" for child in wake
    )
    return (
        f"Spawns you started have finished and you have not read their results: {finished}. "
        "Read them and act on them before you finish."
    )


def _the_ask(blockers: Sequence[DiagnosticBlocker], wake: Sequence[FinishedChild]) -> str:
    """The expiry ask between turns: what is owed, what runs, what closing stops, the answers."""
    descendants, tasks, awaiting = _running_lines(blockers)
    parts = ["This spawn has gone idle."]
    if wake:
        parts.append(_unread_line(wake))
    if descendants or tasks or awaiting:
        running = [f"spawns {', '.join(descendants)}"] if descendants else []
        running += [f"background tasks {'; '.join(tasks)}"] if tasks else []
        running += ["a background task's follow-up result"] if awaiting else []
        parts.append(f"Still running: {', and '.join(running)}.")
        stops = ["cancels those spawns"] if descendants else []
        stops += ["stops those background tasks"] if tasks else []
        if stops:
            parts.append(f"Closing {' and '.join(stops)}.")
    else:
        parts.append("Nothing Meridian can see is running.")
    parts.append(
        "Report where things stand and finish: your reply is this spawn's report, and it "
        f"closes the spawn. Or, to stay, run {_REARM_FORMS}"
    )
    return " ".join(parts)


def nudge_text(
    state: HoldState,
    blockers: Sequence[DiagnosticBlocker],
    wake: tuple[FinishedChild, ...],
    *,
    has_candidate: bool,
    skips_ask: bool,
    active_turn: bool,
) -> str:
    """The text of the ask `decide` just asked for, with `state` as it left it."""
    if state.answer is not None and not active_turn:
        return _the_ask(blockers, wake)
    descendants, tasks, awaiting = _running_lines(blockers)
    details: list[str] = []
    if wake:
        details.append(_unread_line(wake))
    if descendants:
        details.append(
            f"Descendant spawns are still running: {', '.join(descendants)}. "
            "Finishing cancels them."
        )
    if tasks:
        details.append(f"Background tasks are still running: {'; '.join(tasks)}.")
    if awaiting:
        details.append("Background tasks finished, but their follow-up result is pending.")
    if details:
        message = " ".join(details) + " "
    else:
        message = "Before this spawn closes, confirm whether you are actually finished. "
    if has_candidate and not skips_ask and not wake:
        message += (
            "Your previous reply is kept as this spawn's report. If it is the final report, "
            "run `meridian spawn done` and end the turn. "
        )
    else:
        # Codex and OpenCode take this nudge into the running turn, a harness that skips
        # the ask reports every turn, and a wake's answer is new work, so the child's
        # answer becomes the report: a "Done." here would replace it.
        if not has_candidate and not skips_ask:
            message += "No report is kept for this spawn yet. "
        message += (
            "If you are finished, run `meridian spawn done`, then make your last message "
            "the complete report. "
        )
    return message + (
        "If more work remains, including waiting on background tasks or spawns, run "
        f"{_REARM_FORMS} Then finish with a complete report."
    )


__all__ = [
    "BACKEND_DEAD_OUTCOME",
    "IDLE_NUDGE_ANSWER_SECONDS",
    "NATIVE_WAKE_SETTLE_SECONDS",
    "AnswerWindow",
    "AskPhase",
    "HeldLease",
    "HoldFacts",
    "HoldState",
    "HoldStep",
    "SendResult",
    "SpacedLease",
    "TurnEnd",
    "WindowLease",
    "after_event",
    "after_send",
    "after_signals",
    "blocker_labels",
    "decide",
    "defers_close",
    "next_deadline",
    "next_wake",
    "nudge_text",
    "outcome_at_close",
]
