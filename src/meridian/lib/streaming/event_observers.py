"""Post-persist harness event observer delivery."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Protocol

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import RawHarnessEvent

logger = logging.getLogger(__name__)
_OBSERVER_SHUTDOWN_TIMEOUT_SECONDS = 5.0

HarnessEventCallback = Callable[[RawHarnessEvent], Awaitable[None] | None]


class EventObserver(Protocol):
    """Post-persist observer for one spawn's harness events."""

    async def on_event(self, spawn_id: SpawnId, event: RawHarnessEvent) -> None:
        """Observe one persisted harness event."""
        ...

    async def on_complete(self, spawn_id: SpawnId) -> None:
        """Observe drain completion for one spawn."""
        ...


class CallbackObserver:
    """Compatibility adapter for the legacy single-event callback hook."""

    def __init__(self, callback: HarnessEventCallback) -> None:
        self._callback = callback

    async def on_event(self, spawn_id: SpawnId, event: RawHarnessEvent) -> None:
        """Dispatch one event to the wrapped callback."""

        _ = spawn_id
        result = self._callback(event)
        if result is not None:
            await result

    async def on_complete(self, spawn_id: SpawnId) -> None:
        """No-op completion hook for callback compatibility."""

        _ = spawn_id


class QueuedObserver:
    """Bounded non-blocking observer delivery for one spawn.

    The SpawnManager drain loop calls :meth:`enqueue` synchronously after
    persistence. Delivery to the observer happens on this object's background
    task so slow or failing observers do not block event persistence or
    subscriber fan-out.
    """

    def __init__(
        self,
        observer: EventObserver,
        spawn_id: SpawnId,
        max_buffer: int = 1000,
    ) -> None:
        self._observer = observer
        self._spawn_id = spawn_id
        self._queue: asyncio.Queue[RawHarnessEvent | None] = asyncio.Queue(maxsize=max_buffer)
        self._task = asyncio.create_task(self._drain())
        self._completion_signaled = False

    @property
    def task(self) -> asyncio.Task[None]:
        """Return the background drain task."""

        return self._task

    @property
    def observer(self) -> EventObserver:
        """Return the wrapped observer."""

        return self._observer

    def enqueue(self, event: RawHarnessEvent) -> None:
        """Queue one event without blocking; drop and warn if full."""

        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "Observer queue full for spawn %s, dropping event %s",
                self._spawn_id,
                event.event_type,
            )

    def complete(self) -> None:
        """Signal completion, preserving the sentinel under backpressure."""

        if self._completion_signaled:
            return
        self._completion_signaled = True
        while True:
            try:
                self._queue.put_nowait(None)
                return
            except asyncio.QueueFull:
                with suppress(asyncio.QueueEmpty):
                    self._queue.get_nowait()

    def cancel(self) -> None:
        """Cancel delivery without invoking observer completion."""

        if not self._task.done():
            self._task.cancel()

    async def _drain(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                break
            try:
                await self._observer.on_event(self._spawn_id, event)
            except Exception:
                logger.exception("Observer failed for spawn %s", self._spawn_id)
        try:
            await self._observer.on_complete(self._spawn_id)
        except Exception:
            logger.exception("Observer completion failed for spawn %s", self._spawn_id)


class EventObserverRegistry:
    """Per-spawn observer registry with enqueue-and-continue dispatch."""

    def __init__(self, *, max_buffer: int = 1000) -> None:
        self._max_buffer = max_buffer
        self._observers: dict[SpawnId, list[QueuedObserver]] = {}

    def register(self, spawn_id: SpawnId, observer: EventObserver) -> None:
        """Register one observer for a spawn."""

        queued = QueuedObserver(observer, spawn_id, max_buffer=self._max_buffer)
        self._observers.setdefault(spawn_id, []).append(queued)

    def unregister(self, spawn_id: SpawnId, observer: EventObserver) -> None:
        """Remove one observer for a spawn."""

        queued_observers = self._observers.get(spawn_id)
        if not queued_observers:
            return
        remaining: list[QueuedObserver] = []
        for queued in queued_observers:
            if queued.observer is observer:
                queued.cancel()
            else:
                remaining.append(queued)
        if remaining:
            self._observers[spawn_id] = remaining
        else:
            self._observers.pop(spawn_id, None)

    def dispatch(self, spawn_id: SpawnId, event: RawHarnessEvent) -> None:
        """Non-blocking enqueue to all observers registered for a spawn."""

        for queued in self._observers.get(spawn_id, []):
            queued.enqueue(event)

    def complete(self, spawn_id: SpawnId) -> None:
        """Signal completion to all observers for a spawn without waiting."""

        for queued in self._observers.get(spawn_id, []):
            queued.complete()

    async def shutdown(self, spawn_id: SpawnId) -> None:
        """Signal completion and wait for observer delivery tasks to finish."""

        queued_observers = self._observers.pop(spawn_id, [])
        for queued in queued_observers:
            queued.complete()
        for queued in queued_observers:
            with suppress(asyncio.CancelledError):
                try:
                    await asyncio.wait_for(
                        queued.task,
                        timeout=_OBSERVER_SHUTDOWN_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    logger.warning(
                        "Observer shutdown timed out for spawn %s after %.1fs; cancelling",
                        spawn_id,
                        _OBSERVER_SHUTDOWN_TIMEOUT_SECONDS,
                    )
                    queued.task.cancel()
                    with suppress(asyncio.CancelledError):
                        await queued.task


__all__ = [
    "CallbackObserver",
    "EventObserver",
    "EventObserverRegistry",
    "HarnessEventCallback",
    "QueuedObserver",
]
