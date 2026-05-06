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
from meridian.lib.launch.streaming.decision import TerminalEventOutcome

_TERMINAL_EVENT_GRACE_SECONDS = 0.5


class TriggerKind(Enum):
    """Trigger that won terminal arbitration."""

    TERMINAL_FRAME = "terminal_frame"
    COMPLETION = "completion"
    SIGNAL = "signal"
    TIMEOUT = "timeout"
    BUDGET = "budget"
    WATCHDOG = "watchdog"


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
    grace_seconds: float = _TERMINAL_EVENT_GRACE_SECONDS,
) -> ArbitrationDecision:
    """Return the terminal decision for the first completed trigger set.

    Priority order preserves the current streaming-runner semantics:
    1. explicit terminal frame
    2. budget exceeded
    3. timeout
    4. watchdog report termination
    5. completion, including a bounded late-terminal grace window
    6. signal

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

    if completion_task in done:
        return await _completion_decision(terminal_event_future, grace_seconds)

    if signal_task in done:
        # Signal has the lowest precedence.  If completion resolved in the same
        # scheduling turn, preserve the completion/grace behavior.
        if completion_task.done():
            return await _completion_decision(terminal_event_future, grace_seconds)
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


async def _completion_decision(
    terminal_event_future: asyncio.Future[TerminalEventOutcome],
    grace_seconds: float,
) -> ArbitrationDecision:
    terminal_outcome = await _completion_grace(terminal_event_future, grace_seconds)
    if terminal_outcome is not None:
        return _terminal_frame_decision(terminal_outcome)
    return ArbitrationDecision(
        trigger=TriggerKind.COMPLETION,
        terminal_outcome=None,
        stop_required=False,
        synthetic_status=None,
        synthetic_exit_code=None,
        synthetic_error=None,
    )


async def _completion_grace(
    terminal_event_future: asyncio.Future[TerminalEventOutcome],
    grace_seconds: float,
) -> TerminalEventOutcome | None:
    """Wait briefly for a terminal frame after completion."""

    if terminal_event_future.done():
        return terminal_event_future.result()
    try:
        return await asyncio.wait_for(
            asyncio.shield(terminal_event_future),
            timeout=grace_seconds,
        )
    except TimeoutError:
        return None


__all__ = [
    "ArbitrationDecision",
    "TriggerKind",
    "arbitrate_terminal",
]
