"""Shared event-stream liveness policy for managed harness connections."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import TypeVar

_T = TypeVar("_T")


class EventStreamLivenessTimeout(TimeoutError):
    """Raised when a harness event stream stops producing activity."""


class EventStreamLiveness:
    """Track the last observed backend stream activity against a timeout window."""

    def __init__(
        self,
        *,
        timeout_seconds: Callable[[], float],
        now: Callable[[], float],
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._now = now
        self._last_activity_time: float | None = None

    @property
    def last_activity_time(self) -> float | None:
        return self._last_activity_time

    @property
    def is_armed(self) -> bool:
        return self._last_activity_time is not None

    def reset(self) -> None:
        self._last_activity_time = None

    def mark_activity(self) -> None:
        self._last_activity_time = self._now()

    def mark_activity_if_idle(self) -> None:
        if self._last_activity_time is None:
            self.mark_activity()

    def remaining_seconds(self) -> float | None:
        last_activity_time = self._last_activity_time
        if last_activity_time is None:
            return None
        return self._timeout_seconds() - (self._now() - last_activity_time)

    def expired(self) -> bool:
        remaining = self.remaining_seconds()
        return remaining is not None and remaining <= 0

    def healthy(self) -> bool:
        return not self.expired()

    async def wait_for_activity(self, awaitable: Awaitable[_T]) -> _T:
        remaining = self.remaining_seconds()
        if remaining is None:
            return await awaitable
        if remaining <= 0:
            if isinstance(awaitable, Coroutine):
                awaitable.close()
            raise EventStreamLivenessTimeout
        try:
            return await asyncio.wait_for(awaitable, timeout=remaining)
        except TimeoutError as exc:
            raise EventStreamLivenessTimeout from exc


__all__ = ["EventStreamLiveness", "EventStreamLivenessTimeout"]
