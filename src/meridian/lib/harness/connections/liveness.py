"""Shared event-stream liveness policy for managed harness connections."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TypeVar

from meridian.lib.state.liveness import is_process_alive

_T = TypeVar("_T")


class EventStreamLivenessTimeout(TimeoutError):
    """Raised when a harness event stream stops producing activity."""


class LivenessDecision(StrEnum):
    """Structured outcome from a liveness evaluation."""

    CONTINUE = "continue"
    SUPPRESS = "suppress"
    BACKEND_DEAD = "backend_dead"
    STREAM_STALLED = "stream_stalled"


class BackendLivenessPolicy:
    """Composed liveness decision-maker for managed harness backends."""

    def __init__(
        self,
        *,
        timeout_seconds: Callable[[], float],
        now: Callable[[], float],
        backend_pid: Callable[[], int | None],
        backend_birth_time: Callable[[], float | None],
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._now = now
        self._backend_pid = backend_pid
        self._backend_birth_time = backend_birth_time
        # Two clocks, one owner. ``_last_activity_time`` is the last semantic
        # event, and is what a busy bound measures. ``_window_start`` is where
        # the stall timeout measures from; a probe that vouches for ongoing work
        # restarts it without counting as stream activity.
        self._last_activity_time: float | None = None
        self._window_start: float | None = None
        self._busy_vouched = False
        self._unanswered_probes = 0
        self._active_turns: set[str] = set()
        self._active_requests: set[str] = set()
        self._awaiting_done = False

    def reset(self) -> None:
        self._last_activity_time = None
        self._window_start = None
        self._busy_vouched = False
        self._unanswered_probes = 0
        self._active_turns.clear()
        self._active_requests.clear()
        self._awaiting_done = False

    def mark_activity(self) -> None:
        self._last_activity_time = self._now()
        self._window_start = self._last_activity_time
        self._busy_vouched = False
        self._unanswered_probes = 0

    def record_probe(self, answer: bool | None) -> bool:
        """Fold one stall probe into the busy window; report whether the turn runs.

        ``None`` is no usable answer, which a CPU-saturated backend produces by
        pushing the bounded probe past its timeout. Inside a stretch the server
        reported running, one such miss does not end it: the tool being run can
        still deliver its terminal. Two in a row do, so an endpoint that keeps
        failing cannot hold a stream open forever.
        """

        if answer is not None:
            self._busy_vouched = answer
            self._unanswered_probes = 0
            if answer:
                self._window_start = self._now()
            return answer
        if self._busy_vouched and self._unanswered_probes == 0:
            self._unanswered_probes = 1
            self._window_start = self._now()
            return True
        return False

    def silence_seconds(self) -> float:
        """Seconds since the last semantic event, never since the last probe."""

        if self._last_activity_time is None:
            return 0.0
        return self._now() - self._last_activity_time

    def mark_activity_if_idle(self) -> None:
        if self._last_activity_time is None:
            self.mark_activity()

    def signal_turn_started(self, turn_id: str) -> None:
        self._active_turns.add(turn_id)

    def signal_turn_ended(self, turn_id: str) -> None:
        self._active_turns.discard(turn_id)

    def signal_request_in_flight(self, request_id: str) -> None:
        self._active_requests.add(request_id)

    def signal_request_resolved(self, request_id: str) -> None:
        self._active_requests.discard(request_id)

    def set_awaiting_done(self, awaiting_done: bool) -> None:
        self._awaiting_done = awaiting_done

    def evaluate(self) -> LivenessDecision:
        return self._evaluate(suppress_when_awaiting_done=True)

    def classify_close_stream(self) -> LivenessDecision:
        """Classify stream close without suppressing prior idle stream stalls.

        Close classification asks whether a closed stream was dead, stalled, or
        still plausibly active. Awaiting descendant work suppresses ordinary
        adapter health probes but does not turn an already silent idle stream
        into a healthy close.
        """

        return self._evaluate(suppress_when_awaiting_done=False)

    def _evaluate(self, *, suppress_when_awaiting_done: bool) -> LivenessDecision:
        if not self._silence_expired():
            return LivenessDecision.CONTINUE
        pid = self._backend_pid()
        if pid is None:
            return LivenessDecision.BACKEND_DEAD
        if not is_process_alive(pid, created_after_epoch=self._backend_birth_time()):
            return LivenessDecision.BACKEND_DEAD
        if (
            (suppress_when_awaiting_done and self._awaiting_done)
            or self._active_turns
            or self._active_requests
        ):
            return LivenessDecision.SUPPRESS
        return LivenessDecision.STREAM_STALLED

    @property
    def healthy(self) -> bool:
        return self.evaluate() in (LivenessDecision.CONTINUE, LivenessDecision.SUPPRESS)

    async def wait_for_activity(self, awaitable: Awaitable[_T]) -> _T:
        task = asyncio.ensure_future(awaitable)
        try:
            while True:
                decision = self.evaluate()
                if decision in (LivenessDecision.BACKEND_DEAD, LivenessDecision.STREAM_STALLED):
                    # Work that completed at the deadline is progress, not a
                    # stalled stream: deliver it instead of discarding it.
                    task.cancel()
                    try:
                        result = await task
                    except asyncio.CancelledError:
                        current = asyncio.current_task()
                        if current is not None and current.cancelling():
                            raise
                        raise EventStreamLivenessTimeout from None
                    return result

                remaining = self._remaining_seconds()
                if remaining is None:
                    return await task

                wait_timeout = (
                    remaining if decision == LivenessDecision.CONTINUE else self._timeout_seconds()
                )
                if decision == LivenessDecision.CONTINUE and wait_timeout <= 0:
                    continue

                done, _ = await asyncio.wait({task}, timeout=wait_timeout)
                if task in done:
                    return task.result()
        except BaseException:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            raise

    def _silence_expired(self) -> bool:
        remaining = self._remaining_seconds()
        return remaining is not None and remaining <= 0

    def _remaining_seconds(self) -> float | None:
        window_start = self._window_start
        if window_start is None:
            return None
        return self._timeout_seconds() - (self._now() - window_start)


__all__ = ["BackendLivenessPolicy", "EventStreamLivenessTimeout", "LivenessDecision"]
