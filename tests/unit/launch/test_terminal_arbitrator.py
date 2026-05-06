from __future__ import annotations

import asyncio

import pytest

from meridian.lib.launch.streaming.decision import TerminalEventOutcome
from meridian.lib.launch.streaming.terminal_arbitrator import (
    TriggerKind,
    arbitrate_terminal,
)


def _future(result: object | None = None, *, done: bool = False) -> asyncio.Future[object]:
    future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
    if done:
        future.set_result(result)
    return future


def _terminal_future(
    outcome: TerminalEventOutcome | None = None,
) -> asyncio.Future[TerminalEventOutcome]:
    future: asyncio.Future[TerminalEventOutcome] = asyncio.get_running_loop().create_future()
    if outcome is not None:
        future.set_result(outcome)
    return future


@pytest.mark.asyncio
async def test_terminal_frame_wins_immediately() -> None:
    outcome = TerminalEventOutcome(status="succeeded", exit_code=0)

    decision = await arbitrate_terminal(
        completion_task=_future(),
        terminal_event_future=_terminal_future(outcome),
        signal_task=_future(),
    )

    assert decision.trigger is TriggerKind.TERMINAL_FRAME
    assert decision.terminal_outcome == outcome
    assert decision.stop_required is True
    assert decision.synthetic_status == "succeeded"
    assert decision.synthetic_exit_code == 0


@pytest.mark.asyncio
async def test_completion_waits_for_late_terminal_frame_within_grace() -> None:
    loop = asyncio.get_running_loop()
    terminal_future = _terminal_future()
    outcome = TerminalEventOutcome(status="failed", exit_code=1, error="late_frame")
    loop.call_later(0.01, terminal_future.set_result, outcome)

    decision = await arbitrate_terminal(
        completion_task=_future(None, done=True),
        terminal_event_future=terminal_future,
        signal_task=_future(),
        grace_seconds=0.1,
    )

    assert decision.trigger is TriggerKind.TERMINAL_FRAME
    assert decision.terminal_outcome == outcome
    assert decision.stop_required is True


@pytest.mark.asyncio
async def test_completion_without_late_terminal_frame_completes_normally() -> None:
    decision = await arbitrate_terminal(
        completion_task=_future(None, done=True),
        terminal_event_future=_terminal_future(),
        signal_task=_future(),
        grace_seconds=0.001,
    )

    assert decision.trigger is TriggerKind.COMPLETION
    assert decision.terminal_outcome is None
    assert decision.stop_required is False


@pytest.mark.asyncio
async def test_signal_only_requests_cancel_stop() -> None:
    decision = await arbitrate_terminal(
        completion_task=_future(),
        terminal_event_future=_terminal_future(),
        signal_task=_future(True, done=True),
    )

    assert decision.trigger is TriggerKind.SIGNAL
    assert decision.stop_required is True
    assert decision.synthetic_status == "cancelled"
    assert decision.synthetic_exit_code is None
    assert decision.synthetic_error == "cancelled"


@pytest.mark.asyncio
async def test_budget_beats_completion_when_both_ready() -> None:
    decision = await arbitrate_terminal(
        completion_task=_future(None, done=True),
        terminal_event_future=_terminal_future(),
        signal_task=_future(),
        budget_task=_future(True, done=True),
        grace_seconds=0.001,
    )

    assert decision.trigger is TriggerKind.BUDGET
    assert decision.stop_required is True
    assert decision.synthetic_status == "failed"
    assert decision.synthetic_exit_code is None
    assert decision.synthetic_error == "budget_exceeded"


@pytest.mark.asyncio
async def test_timeout_beats_late_terminal_frame() -> None:
    loop = asyncio.get_running_loop()
    terminal_future = _terminal_future()
    loop.call_later(
        0.01,
        terminal_future.set_result,
        TerminalEventOutcome(status="succeeded", exit_code=0),
    )

    decision = await arbitrate_terminal(
        completion_task=_future(),
        terminal_event_future=terminal_future,
        signal_task=_future(),
        timeout_task=_future(None, done=True),
        grace_seconds=0.1,
    )

    assert decision.trigger is TriggerKind.TIMEOUT
    assert decision.stop_required is True
    assert decision.synthetic_status == "failed"
    assert decision.synthetic_exit_code == 3
    assert decision.synthetic_error == "timeout"


@pytest.mark.asyncio
async def test_watchdog_false_is_noop_decision() -> None:
    decision = await arbitrate_terminal(
        completion_task=_future(),
        terminal_event_future=_terminal_future(),
        signal_task=_future(),
        watchdog_task=_future(False, done=True),
    )

    assert decision.trigger is TriggerKind.WATCHDOG
    assert decision.stop_required is False
    assert decision.watchdog_noop is True


@pytest.mark.asyncio
async def test_watchdog_true_reports_watchdog_trigger() -> None:
    decision = await arbitrate_terminal(
        completion_task=_future(),
        terminal_event_future=_terminal_future(),
        signal_task=_future(),
        watchdog_task=_future(True, done=True),
    )

    assert decision.trigger is TriggerKind.WATCHDOG
    assert decision.stop_required is False
    assert decision.watchdog_noop is False


@pytest.mark.asyncio
async def test_completion_beats_signal_when_both_ready() -> None:
    decision = await arbitrate_terminal(
        completion_task=_future(None, done=True),
        terminal_event_future=_terminal_future(),
        signal_task=_future(True, done=True),
        grace_seconds=0.001,
    )

    assert decision.trigger is TriggerKind.COMPLETION
    assert decision.stop_required is False


@pytest.mark.asyncio
async def test_optional_triggers_are_absent_when_not_passed() -> None:
    # This would be a timeout if the optional timeout future were passed.  Leaving
    # it absent keeps the minimal runner race limited to terminal/completion/signal.
    decision = await arbitrate_terminal(
        completion_task=_future(),
        terminal_event_future=_terminal_future(),
        signal_task=_future(True, done=True),
        timeout_task=None,
        budget_task=None,
        watchdog_task=None,
    )

    assert decision.trigger is TriggerKind.SIGNAL
