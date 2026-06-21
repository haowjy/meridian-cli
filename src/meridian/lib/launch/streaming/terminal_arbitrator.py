"""Single terminal-outcome arbitration for streaming spawns.

Both ``run_streaming_spawn()`` and ``_run_streaming_attempt()`` delegate
terminal precedence decisions to this module.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any, cast

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.harness.semantics import TerminalEventOutcome


class TriggerKind(Enum):
    """Trigger that won terminal arbitration."""

    TERMINAL_FRAME = "terminal_frame"
    COMPLETION = "completion"
    SIGNAL = "signal"
    TIMEOUT = "timeout"
    BUDGET = "budget"
    WATCHDOG = "watchdog"
    INACTIVITY = "inactivity"


@dataclass(frozen=True)
class ArbitrationDecision:
    """Outcome of the terminal trigger race."""

    trigger: TriggerKind
    terminal_outcome: TerminalEventOutcome | None
    stop_required: bool
    synthetic_status: SpawnStatus | None
    synthetic_exit_code: int | None
    synthetic_error: str | None
    watchdog_noop: bool = False


async def arbitrate_terminal(
    *,
    completion_task: asyncio.Future[Any],
    terminal_event_future: asyncio.Future[TerminalEventOutcome],
    signal_task: asyncio.Future[Any],
    timeout_task: asyncio.Future[None] | None = None,
    budget_task: asyncio.Future[Any] | None = None,
    watchdog_task: asyncio.Future[bool] | None = None,
    inactivity_task: asyncio.Future[bool] | None = None,
) -> ArbitrationDecision:
    """Return the terminal decision for the first completed trigger set.

    Priority order preserves the current streaming-runner semantics:
    1. explicit terminal frame
    2. budget exceeded
    3. timeout
    4. watchdog report termination
    5. inactivity stall termination
    6. completion
    7. signal

    Optional triggers are absent from the race when their task is ``None``.
    """

    wait_tasks: set[asyncio.Future[object]] = {
        cast("asyncio.Future[object]", completion_task),
        cast("asyncio.Future[object]", signal_task),
        cast("asyncio.Future[object]", terminal_event_future),
    }
    if budget_task is not None:
        wait_tasks.add(cast("asyncio.Future[object]", budget_task))
    if timeout_task is not None:
        wait_tasks.add(cast("asyncio.Future[object]", timeout_task))
    if watchdog_task is not None:
        wait_tasks.add(cast("asyncio.Future[object]", watchdog_task))
    if inactivity_task is not None:
        wait_tasks.add(cast("asyncio.Future[object]", inactivity_task))

    done, _ = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)

    if terminal_event_future in done:
        return _terminal_frame_decision(terminal_event_future.result())

    if budget_task is not None and budget_task in done:
        return ArbitrationDecision(
            trigger=TriggerKind.BUDGET,
            terminal_outcome=None,
            stop_required=True,
            synthetic_status="failed",
            synthetic_exit_code=None,
            synthetic_error="budget_exceeded",
        )

    if timeout_task is not None and timeout_task in done:
        if terminal_event_future.done():
            return _terminal_frame_decision(terminal_event_future.result())
        if completion_task.done():
            return _completion_decision(terminal_event_future)
        return ArbitrationDecision(
            trigger=TriggerKind.TIMEOUT,
            terminal_outcome=None,
            stop_required=True,
            synthetic_status="failed",
            synthetic_exit_code=3,
            synthetic_error="timeout",
        )

    if watchdog_task is not None and watchdog_task in done:
        watchdog_stopped_spawn = watchdog_task.result()
        return ArbitrationDecision(
            trigger=TriggerKind.WATCHDOG,
            terminal_outcome=None,
            stop_required=False,
            synthetic_status=None,
            synthetic_exit_code=None,
            synthetic_error=None,
            watchdog_noop=not watchdog_stopped_spawn,
        )

    if inactivity_task is not None and inactivity_task in done:
        stopped = inactivity_task.result()
        return ArbitrationDecision(
            trigger=TriggerKind.INACTIVITY,
            terminal_outcome=None,
            stop_required=False,
            synthetic_status=None,
            synthetic_exit_code=None,
            synthetic_error=None,
            watchdog_noop=not stopped,
        )

    if completion_task in done:
        return _completion_decision(terminal_event_future)

    if signal_task in done:
        # Signal has the lowest precedence.  If completion resolved in the same
        # scheduling turn, preserve completion-derived behavior.
        if completion_task.done():
            return _completion_decision(terminal_event_future)
        return ArbitrationDecision(
            trigger=TriggerKind.SIGNAL,
            terminal_outcome=None,
            stop_required=True,
            synthetic_status="cancelled",
            synthetic_exit_code=None,
            synthetic_error="cancelled",
        )

    raise RuntimeError("unreachable terminal arbitration state")


def _terminal_frame_decision(outcome: TerminalEventOutcome) -> ArbitrationDecision:
    return ArbitrationDecision(
        trigger=TriggerKind.TERMINAL_FRAME,
        terminal_outcome=outcome,
        stop_required=True,
        synthetic_status=outcome.status,
        synthetic_exit_code=outcome.exit_code,
        synthetic_error=outcome.error,
    )


def _completion_decision(
    terminal_event_future: asyncio.Future[TerminalEventOutcome],
) -> ArbitrationDecision:
    if terminal_event_future.done():
        return _terminal_frame_decision(terminal_event_future.result())
    return ArbitrationDecision(
        trigger=TriggerKind.COMPLETION,
        terminal_outcome=None,
        stop_required=False,
        synthetic_status=None,
        synthetic_exit_code=None,
        synthetic_error=None,
    )


__all__ = [
    "ArbitrationDecision",
    "TriggerKind",
    "arbitrate_terminal",
]
