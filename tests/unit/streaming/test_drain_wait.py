"""Drain input waiter tests."""

from __future__ import annotations

import asyncio

import pytest

from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.streaming.drain_wait import DrainInputWaiter
from meridian.lib.streaming.spawn_drain_loop import resolve_terminal_outcome
from meridian.lib.streaming.spawn_session import DrainOutcome


class _FailingDiskDrain:
    def wants_aux_wake(self) -> bool:
        return True

    async def wait_for_aux_wake(self) -> None:
        raise RuntimeError("disk watcher failed")


async def _never_events():  # type: ignore[no-untyped-def]
    await asyncio.sleep(60)
    yield RawHarnessEvent(event_type="never", harness_id="pi", payload={})


@pytest.mark.asyncio
async def test_drain_waiter_propagates_disk_wait_failure() -> None:
    waiter = DrainInputWaiter(
        _never_events().__aiter__(),
        _FailingDiskDrain(),
    )

    try:
        with pytest.raises(RuntimeError, match="disk watcher failed"):
            await waiter.wait(None)
    finally:
        await waiter.close()


def test_terminal_outcome_priority_is_success_then_stop_then_drain() -> None:
    succeeded = DrainOutcome(status="succeeded", exit_code=0)
    stopped = DrainOutcome(status="timed_out", exit_code=3, error="timeout")
    failed = DrainOutcome(status="failed", exit_code=1, error="drain failed")

    assert resolve_terminal_outcome(succeeded, stopped) is succeeded
    assert resolve_terminal_outcome(failed, stopped) is stopped
    assert resolve_terminal_outcome(failed, None) is failed
