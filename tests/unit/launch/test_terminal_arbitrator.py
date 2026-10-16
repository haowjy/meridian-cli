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


_SUCCESS = TerminalEventOutcome(status="succeeded", exit_code=0)
_FAILURE = TerminalEventOutcome(status="failed", exit_code=1, error="late_frame")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ready", "expected"),
    [
        pytest.param(
            {
                "completion": True,
                "terminal": _SUCCESS,
                "watchdog": True,
            },
            {
                "trigger": TriggerKind.TERMINAL_FRAME,
                "terminal_outcome": _SUCCESS,
                "stop_required": True,
                "synthetic_status": "succeeded",
                "synthetic_exit_code": 0,
            },
            id="terminal-frame-wins-immediately",
        ),
        pytest.param(
            {"completion": True, "terminal": _FAILURE},
            {
                "trigger": TriggerKind.TERMINAL_FRAME,
                "terminal_outcome": _FAILURE,
                "stop_required": True,
            },
            id="completion-uses-observed-terminal-frame",
        ),
        pytest.param(
            {"completion": True},
            {
                "trigger": TriggerKind.COMPLETION,
                "terminal_outcome": None,
                "stop_required": False,
            },
            id="completion-without-terminal-frame",
        ),
        pytest.param(
            {"signal": True},
            {
                "trigger": TriggerKind.SIGNAL,
                "stop_required": True,
                "synthetic_status": "cancelled",
                "synthetic_exit_code": None,
                "synthetic_error": "cancelled",
            },
            id="signal-requests-cancel-stop",
        ),
        pytest.param(
            {"completion": True, "budget": True},
            {
                "trigger": TriggerKind.BUDGET,
                "stop_required": True,
                "synthetic_status": "failed",
                "synthetic_exit_code": None,
                "synthetic_error": "budget_exceeded",
            },
            id="budget-beats-completion",
        ),
        pytest.param(
            {"timeout": None},
            {
                "trigger": TriggerKind.TIMEOUT,
                "stop_required": True,
                "synthetic_status": "failed",
                "synthetic_exit_code": 3,
                "synthetic_error": "timeout",
            },
            id="timeout",
        ),
        pytest.param(
            {"terminal": _SUCCESS, "timeout": None},
            {
                "trigger": TriggerKind.TERMINAL_FRAME,
                "terminal_outcome": _SUCCESS,
                "stop_required": True,
            },
            id="timeout-prefers-terminal",
        ),
        pytest.param(
            {"completion": True, "timeout": None},
            {"trigger": TriggerKind.COMPLETION, "stop_required": False},
            id="timeout-prefers-completion",
        ),
        pytest.param(
            {"completion": True, "watchdog": False},
            {"trigger": TriggerKind.COMPLETION, "stop_required": False},
            id="completion-beats-noop-watchdog",
        ),
        pytest.param(
            {"completion": True, "watchdog": True},
            {"trigger": TriggerKind.WATCHDOG, "stop_required": False},
            id="watchdog",
        ),
        pytest.param(
            {"completion": True, "inactivity": True},
            {"trigger": TriggerKind.INACTIVITY, "stop_required": False},
            id="inactivity",
        ),
        pytest.param(
            {"completion": True, "signal": True},
            {"trigger": TriggerKind.COMPLETION, "stop_required": False},
            id="completion-beats-signal",
        ),
    ],
)
async def test_terminal_arbitration(
    ready: dict[str, object],
    expected: dict[str, object],
) -> None:
    kwargs = {
        "completion_task": _future(None, done=ready.get("completion") is True),
        "terminal_event_future": _terminal_future(ready.get("terminal")),  # type: ignore[arg-type]
        "signal_task": _future(True, done=ready.get("signal") is True),
    }
    for name in ("budget", "timeout", "watchdog", "inactivity"):
        if name in ready:
            kwargs[f"{name}_task"] = _future(ready[name], done=True)

    decision = await arbitrate_terminal(**kwargs)  # type: ignore[arg-type]

    for field, value in expected.items():
        assert getattr(decision, field) == value
