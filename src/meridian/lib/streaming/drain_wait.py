"""Async wait arbitration for streaming drain loops."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.streaming.drain_coordinator import AuxWakeCoordinator


@dataclass(frozen=True)
class DrainEventWake:
    event: HarnessEvent
    disk_change_ready_after_event: bool = False


@dataclass(frozen=True)
class DrainAuxWake:
    pass


@dataclass(frozen=True)
class DrainTimeoutWake:
    pass


@dataclass(frozen=True)
class DrainClosedWake:
    pass


DrainWake = DrainEventWake | DrainAuxWake | DrainTimeoutWake | DrainClosedWake


class DrainInputWaiter:
    """Wait for the next harness event, auxiliary wake, timeout, or stream close."""

    def __init__(
        self,
        events_iter: AsyncIterator[HarnessEvent],
        aux_wake: AuxWakeCoordinator,
    ) -> None:
        self._events_iter = events_iter
        self._aux_wake = aux_wake
        self._pending_event_task: asyncio.Future[HarnessEvent] | None = None
        self._pending_disk_task: asyncio.Task[None] | None = None

    async def wait(self, timeout_seconds: float | None) -> DrainWake:
        timeout_task: asyncio.Task[None] | None = None
        try:
            if self._pending_event_task is None:
                self._pending_event_task = asyncio.ensure_future(anext(self._events_iter))
            if self._pending_event_task.done():
                disk_change_ready_after_event = self._consume_ready_disk_change()
                event = self._pending_event_task.result()
                self._pending_event_task = None
                return DrainEventWake(event, disk_change_ready_after_event)

            wait_tasks: set[asyncio.Future[Any]] = {self._pending_event_task}
            if self._aux_wake.wants_aux_wake():
                if self._pending_disk_task is None:
                    self._pending_disk_task = asyncio.create_task(
                        self._aux_wake.wait_for_aux_wake()
                    )
                elif self._pending_disk_task.done():
                    self._pending_disk_task.result()
                    self._pending_disk_task = None
                    return DrainAuxWake()
                wait_tasks.add(self._pending_disk_task)
            if timeout_seconds is not None:
                timeout_task = asyncio.create_task(asyncio.sleep(timeout_seconds))
                wait_tasks.add(timeout_task)

            if len(wait_tasks) == 1:
                event = await self._pending_event_task
                self._pending_event_task = None
                return DrainEventWake(event)

            done, _pending = await asyncio.wait(
                wait_tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._pending_event_task in done:
                disk_change_ready_after_event = self._consume_ready_disk_change()
                event = self._pending_event_task.result()
                self._pending_event_task = None
                return DrainEventWake(event, disk_change_ready_after_event)
            if self._pending_disk_task is not None and self._pending_disk_task in done:
                self._pending_disk_task.result()
                self._pending_disk_task = None
                return DrainAuxWake()
            return DrainTimeoutWake()
        except StopAsyncIteration:
            self._pending_event_task = None
            return DrainClosedWake()
        finally:
            await _cancel_task(timeout_task)

    def _consume_ready_disk_change(self) -> bool:
        if self._pending_disk_task is None or not self._pending_disk_task.done():
            return False
        self._pending_disk_task.result()
        self._pending_disk_task = None
        return True

    async def close(self) -> None:
        await _cancel_task(self._pending_event_task)
        await _cancel_task(self._pending_disk_task)


async def _cancel_task(task: asyncio.Future[Any] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
