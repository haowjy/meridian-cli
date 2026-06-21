"""Deterministic async test helpers — avoid wall-clock sleeps in flaky tests.

API
---
``AsyncDeterminism``
    Bundles ``FakeClock`` with installable ``time.monotonic`` / ``asyncio.sleep``
    patches.  ``install()`` patches the event-loop clock and replaces
    ``asyncio.sleep`` so delays advance the fake clock and yield once.

``install_monotonic(monkeypatch, clock, *modules)``
    Patch ``time.monotonic`` on production modules that read wall time directly.

``yield_to_loop()``
    Yield one event-loop tick (``asyncio.sleep(0)``) without advancing fake time.

``wait_until(predicate, *, timeout=1.0, on_tick=None)``
    Poll *predicate* until true, yielding each tick.  Optional *on_tick* runs
    after each yield (e.g. ``clock.advance``).

``wait_while(predicate, *, timeout=1.0, on_tick=None)``
    Poll until *predicate* is false.

``assert_still_pending(task)``
    Assert an ``asyncio.Task`` has not completed yet (replaces sleep-then-assert).

``TaskGate``
    Barrier: ``await gate.wait_open()`` blocks until ``gate.open()`` is called.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

from tests.support.fakes import FakeClock

_real_asyncio_sleep = asyncio.sleep


async def yield_to_loop() -> None:
    """Yield control to the event loop without advancing fake time."""
    await _real_asyncio_sleep(0)


async def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 1.0,
    on_tick: Callable[[], None] | None = None,
    description: str = "condition",
) -> None:
    """Wait until *predicate* returns true, yielding between polls."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if on_tick is not None:
            on_tick()
        await yield_to_loop()
    raise AssertionError(f"timed out waiting for {description}")


async def wait_while(
    predicate: Callable[[], bool],
    *,
    timeout: float = 1.0,
    on_tick: Callable[[], None] | None = None,
    description: str = "condition to clear",
) -> None:
    """Wait until *predicate* returns false."""
    await wait_until(
        lambda: not predicate(),
        timeout=timeout,
        on_tick=on_tick,
        description=f"clearance of {description}",
    )


async def assert_still_pending(task: asyncio.Task[Any]) -> None:
    """Assert *task* has not completed; yield once so concurrent work can run."""
    await yield_to_loop()
    assert not task.done(), "expected task to still be pending"


def install_monotonic(
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    *modules: ModuleType,
) -> None:
    """Patch ``time.monotonic`` on each module to read from *clock*."""
    for module in modules:
        monkeypatch.setattr(module.time, "monotonic", clock.monotonic)


class AsyncDeterminism:
    """Fake-clock harness for async tests that would otherwise sleep."""

    def __init__(self, start: float = 0.0) -> None:
        self.clock = FakeClock(start=start)

    def install(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        monotonic_modules: tuple[ModuleType, ...] = (),
    ) -> None:
        install_monotonic(monkeypatch, self.clock, *monotonic_modules)
        monkeypatch.setattr(asyncio, "sleep", self._fake_sleep)

    def install_on_running_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patch the already-running loop's ``time()`` (call from inside async test)."""
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "time", self.clock.monotonic)

    async def sleep(self, delay: float = 0.0) -> None:
        """Advance the fake clock by *delay* and yield one event-loop tick."""
        await self._fake_sleep(delay)

    async def _fake_sleep(self, delay: float = 0.0) -> None:
        if delay > 0:
            self.clock.advance(delay)
        await _real_asyncio_sleep(0)

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)


class TaskGate:
    """Manual barrier: waiters block until ``open()`` is called."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def open(self) -> None:
        self._event.set()

    def close(self) -> None:
        self._event.clear()

    async def wait_open(self) -> None:
        await self._event.wait()
