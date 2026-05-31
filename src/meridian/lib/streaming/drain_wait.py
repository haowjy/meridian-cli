"""Async wait arbitration for streaming drain loops."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.streaming.pi_drain import PiDrainCoordinator


@dataclass(frozen=True)
class DrainEventWake:
    event: HarnessEvent
    disk_change_ready_after_event: bool = False


@dataclass(frozen=True)
class DrainDiskChangeWake:
    pass


@dataclass(frozen=True)
class DrainTimeoutWake:
    pass


@dataclass(frozen=True)
class DrainClosedWake:
    pass


DrainWake = DrainEventWake | DrainDiskChangeWake | DrainTimeoutWake | DrainClosedWake


class DrainInputWaiter:
    """Wait for the next harness event, Pi disk change, timeout, or stream close."""

    def __init__(
        self,
        events_iter: AsyncIterator[HarnessEvent],
        pi_drain: PiDrainCoordinator,
    ) -> None:
        self._events_iter = events_iter
        self._pi_drain = pi_drain
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
            if self._pi_drain.quiescence_enabled:
                if self._pending_disk_task is None:
                    self._pending_disk_task = asyncio.create_task(
                        self._pi_drain.wait_for_disk_change()
                    )
                elif self._pending_disk_task.done():
                    self._pending_disk_task.result()
                    self._pending_disk_task = None
                    return DrainDiskChangeWake()
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
                return DrainDiskChangeWake()
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
