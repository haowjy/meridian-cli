"""Spawn drain persistence-ordering regression tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import Mock

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.state.history import WriteResult
from meridian.lib.streaming.drain_coordinator import (
    DrainExitDecision,
    DrainLoopDecision,
    DrainPlan,
)
from meridian.lib.streaming.spawn_drain_loop import SpawnDrainLoop


@pytest.mark.asyncio
async def test_failed_history_write_blocks_post_persist_delivery() -> None:
    spawn_id = SpawnId("p-persist-order")
    failed_event = HarnessEvent(event_type="message", harness_id="test", payload={"id": 1})
    persisted_event = HarnessEvent(
        event_type="message",
        harness_id="test",
        payload={"id": 2},
    )
    calls: list[tuple[str, HarnessEvent]] = []

    class Receiver:
        primary_event_scope = None

        async def events(self) -> AsyncIterator[HarnessEvent]:
            yield failed_event
            yield persisted_event

    class HistoryWriter:
        def __init__(self) -> None:
            self._results = iter(
                [
                    WriteResult(success=False, error="transient write failure"),
                    WriteResult(success=True, seq=0),
                ]
            )

        def write(self, event: HarnessEvent) -> WriteResult:
            calls.append(("persist", event))
            return next(self._results)

    class Coordinator:
        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        def next_timeout(self) -> None:
            return None

        async def observe_event(self, event: HarnessEvent, transition: str | None) -> bool:
            _ = transition
            calls.append(("pre_persist", event))
            return False

        def note_event_persisted(self, event: HarnessEvent) -> DrainLoopDecision:
            calls.append(("noted", event))
            return DrainLoopDecision()

        async def after_event(self) -> DrainLoopDecision:
            return DrainLoopDecision()

        def handle_close(self, *, intentional_stop: bool) -> None:
            _ = intentional_stop
            return None

        async def handle_stream_exit(
            self,
            recorded_outcome: Any,
        ) -> DrainExitDecision:
            return DrainExitDecision(recorded_outcome=recorded_outcome)

    def record_observer_dispatch(_spawn_id: SpawnId, event: HarnessEvent) -> None:
        calls.append(("observe", event))

    observers = Mock()
    observers.dispatch.side_effect = record_observer_dispatch
    loop = SpawnDrainLoop(
        sessions={},
        history_writers={spawn_id: HistoryWriter()},  # type: ignore[dict-item]
        observers=observers,
        cleanup_tasks=set(),
        cleanup_completed_session=Mock(),
        resolve_completion_future=Mock(),
        fan_out_event=lambda _spawn_id, event: calls.append(("fan_out", event)),
        fan_out_turn_boundary=Mock(),
    )

    await loop.run(
        spawn_id=spawn_id,
        receiver=Receiver(),  # type: ignore[arg-type]
        drain_plan=DrainPlan(coordinator=Coordinator()),  # type: ignore[arg-type]
        tracer=None,
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
