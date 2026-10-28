from __future__ import annotations

import asyncio

import pytest

from meridian.lib.harness.semantics import TerminalEventOutcome
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


@pytest.mark.parametrize(
    ("outcome", "completion_done", "competitor"),
    [
        (
            TerminalEventOutcome(status="succeeded", exit_code=0),
            True,
            "watchdog",
        ),
        (
            TerminalEventOutcome(status="failed", exit_code=1, error="late_frame"),
            True,
            None,
        ),
        (
            TerminalEventOutcome(status="succeeded", exit_code=0),
            False,
            "timeout",
        ),
    ],
    ids=["observed-and-succeeded", "observed-late-frame", "timeout-already-done"],
)
@pytest.mark.asyncio
async def test_terminal_frame_wins_immediately(
    outcome: TerminalEventOutcome,
    completion_done: bool,
    competitor: str | None,
) -> None:
    kwargs: dict[str, object] = {
        "completion_task": _future(None, done=completion_done),
        "terminal_event_future": _terminal_future(outcome),
        "signal_task": _future(),
    }
    if competitor == "watchdog":
        kwargs["watchdog_task"] = _future(True, done=True)
    elif competitor == "timeout":
        kwargs["timeout_task"] = _future(None, done=True)

    decision = await arbitrate_terminal(**kwargs)  # type: ignore[arg-type]

    assert decision.trigger is TriggerKind.TERMINAL_FRAME
    assert decision.terminal_outcome == outcome
    assert decision.stop_required is True
    assert decision.synthetic_status == outcome.status
    assert decision.synthetic_exit_code == outcome.exit_code
    assert decision.synthetic_error == outcome.error


@pytest.mark.parametrize(
    "competitor",
    [None, "timeout", "watchdog", "inactivity", "signal"],
)
@pytest.mark.asyncio
async def test_completion_without_terminal_frame_completes_normally(
    competitor: str | None,
) -> None:
    kwargs: dict[str, object] = {
        "completion_task": _future(None, done=True),
        "terminal_event_future": _terminal_future(),
        "signal_task": _future(True, done=True) if competitor == "signal" else _future(),
    }
    if competitor == "timeout":
        kwargs["timeout_task"] = _future(None, done=True)
    elif competitor == "watchdog":
        kwargs["watchdog_task"] = _future(False, done=True)
    elif competitor == "inactivity":
        kwargs["inactivity_task"] = _future(False, done=True)

    decision = await arbitrate_terminal(**kwargs)  # type: ignore[arg-type]

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
    )

    assert decision.trigger is TriggerKind.BUDGET
    assert decision.stop_required is True
    assert decision.synthetic_status == "failed"
    assert decision.synthetic_exit_code is None
    assert decision.synthetic_error == "budget_exceeded"


@pytest.mark.asyncio
async def test_timeout_returns_timeout_when_no_terminal_or_completion() -> None:
    decision = await arbitrate_terminal(
        completion_task=_future(),
        terminal_event_future=_terminal_future(),
        signal_task=_future(),
        timeout_task=_future(None, done=True),
    )

    assert decision.trigger is TriggerKind.TIMEOUT
    assert decision.stop_required is True
    assert decision.synthetic_status == "failed"
    assert decision.synthetic_exit_code == 3
    assert decision.synthetic_error == "timeout"


@pytest.mark.asyncio
async def test_watchdog_true_reports_watchdog_trigger() -> None:
    decision = await arbitrate_terminal(
        completion_task=_future(None, done=True),
        terminal_event_future=_terminal_future(),
        signal_task=_future(),
        watchdog_task=_future(True, done=True),
    )

    assert decision.trigger is TriggerKind.WATCHDOG
    assert decision.stop_required is False


@pytest.mark.asyncio
async def test_inactivity_true_reports_inactivity_trigger() -> None:
    decision = await arbitrate_terminal(
        completion_task=_future(None, done=True),
        terminal_event_future=_terminal_future(),
        signal_task=_future(),
        inactivity_task=_future(True, done=True),
    )

    assert decision.trigger is TriggerKind.INACTIVITY
    assert decision.stop_required is False
