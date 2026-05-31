"""Drain input waiter tests."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.streaming.drain_wait import DrainInputWaiter
from meridian.lib.streaming.pi_drain import PiDrainCoordinator


class _FailingDiskDrain:
    quiescence_enabled = True

    async def wait_for_disk_change(self) -> None:
        raise RuntimeError("disk watcher failed")


async def _never_events():  # type: ignore[no-untyped-def]
    await asyncio.sleep(60)
    yield HarnessEvent(event_type="never", harness_id="pi", payload={})


@pytest.mark.asyncio
async def test_drain_waiter_propagates_disk_wait_failure() -> None:
    waiter = DrainInputWaiter(
        _never_events().__aiter__(),
        cast("PiDrainCoordinator", _FailingDiskDrain()),
    )

    try:
        with pytest.raises(RuntimeError, match="disk watcher failed"):
            await asyncio.wait_for(waiter.wait(None), timeout=1.0)
    finally:
        await waiter.close()
