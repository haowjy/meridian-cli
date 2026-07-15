"""Spawn drain persistence-ordering regression tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import HarnessConnection, HarnessEvent
from meridian.lib.state.history import HarnessHistoryWriter, WriteResult
from meridian.lib.streaming.drain_coordinator import (
    DrainCoordinator,
    DrainExitDecision,
    DrainLoopDecision,
    DrainPlan,
)
from meridian.lib.streaming.event_observers import EventObserverRegistry
from meridian.lib.streaming.spawn_drain_loop import SpawnDrainLoop

_SPAWN_ID = SpawnId("p-persist-order")
Call = tuple[str, HarnessEvent]


class _Receiver:
    primary_event_scope = None

    def __init__(self, events: list[HarnessEvent]) -> None:
        self._events = events

    async def events(self) -> AsyncIterator[HarnessEvent]:
        for event in self._events:
            yield event


class _HistoryWriter:
    def __init__(self, results: list[WriteResult], calls: list[Call]) -> None:
        self._results = iter(results)
        self._calls = calls

    def write(self, event: HarnessEvent) -> WriteResult:
        self._calls.append(("persist", event))
        return next(self._results)


class _Coordinator:
    def __init__(self, calls: list[Call]) -> None:
        self._calls = calls

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def next_timeout(self) -> None:
        return None

    async def observe_event(self, event: HarnessEvent, transition: str | None) -> bool:
        _ = transition
        self._calls.append(("pre_persist", event))
        return False

    def note_event_persisted(self, event: HarnessEvent) -> DrainLoopDecision:
        self._calls.append(("noted", event))
        return DrainLoopDecision()

    async def after_event(self) -> DrainLoopDecision:
        return DrainLoopDecision()

    def handle_close(self, *, intentional_stop: bool) -> None:
        _ = intentional_stop
        return None

    async def handle_stream_exit(self, recorded_outcome: Any) -> DrainExitDecision:
        return DrainExitDecision(recorded_outcome=recorded_outcome)


async def _run_drain(events: list[HarnessEvent], results: list[WriteResult]) -> list[Call]:
    calls: list[Call] = []

    def record_observer_dispatch(_spawn_id: SpawnId, event: HarnessEvent) -> None:
        calls.append(("observe", event))

    observers = Mock()
    observers.dispatch.side_effect = record_observer_dispatch
    loop = SpawnDrainLoop(
        sessions={},
        history_writers={
            _SPAWN_ID: cast("HarnessHistoryWriter", _HistoryWriter(results, calls))
        },
        observers=cast("EventObserverRegistry", observers),
        cleanup_tasks=set(),
        cleanup_completed_session=AsyncMock(),
        resolve_completion_future=Mock(),
        fan_out_event=lambda _spawn_id, event: calls.append(("fan_out", event)),
        fan_out_turn_boundary=AsyncMock(),
    )
    coordinator = cast("DrainCoordinator", _Coordinator(calls))

    await loop.run(
        spawn_id=_SPAWN_ID,
        receiver=cast("HarnessConnection[Any]", _Receiver(events)),
        drain_plan=DrainPlan(coordinator=coordinator),
        tracer=None,
    )
    return calls


@pytest.mark.asyncio
async def test_failed_history_write_blocks_post_persist_delivery() -> None:
    failed_event = HarnessEvent(event_type="message", harness_id="test", payload={"id": 1})
    persisted_event = HarnessEvent(
        event_type="message",
        harness_id="test",
        payload={"id": 2},
    )

    calls = await _run_drain(
        [failed_event, persisted_event],
        [
            WriteResult(success=False, error="transient write failure"),
            WriteResult(success=True, seq=0),
        ],
    )

    assert calls == [
        ("pre_persist", failed_event),
        ("persist", failed_event),
        ("pre_persist", persisted_event),
        ("persist", persisted_event),
        ("observe", persisted_event),
        ("fan_out", persisted_event),
        ("noted", persisted_event),
    ]


@pytest.mark.asyncio
async def test_tenth_history_write_failure_aborts_without_delivery() -> None:
    events = [
        HarnessEvent(event_type="message", harness_id="test", payload={"id": index})
        for index in range(11)
    ]

    calls = await _run_drain(
        events,
        [WriteResult(success=False, error="write failure") for _ in events],
    )

    assert calls == [
        call
        for event in events[:10]
        for call in (("pre_persist", event), ("persist", event))
    ]
