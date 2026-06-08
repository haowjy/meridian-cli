"""Spawn drain loop coordinator seam tests."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    ConnectionState,
    HarnessConnection,
    HarnessEvent,
    StopProgressCallback,
    StopResult,
)
from meridian.lib.harness.semantics import TerminalEventOutcome
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.streaming.drain_coordinator import (
    DrainExitDecision,
    DrainLoopDecision,
    DrainTerminalDecision,
)
from meridian.lib.streaming.drain_policy import DrainAction, DrainPolicy, SingleTurnDrainPolicy
from meridian.lib.streaming.event_observers import EventObserverRegistry
from meridian.lib.streaming.spawn_drain_loop import SpawnDrainLoop
from meridian.lib.streaming.spawn_session import DrainOutcome, SpawnSession


class _FakeConnection(HarnessConnection[ResolvedLaunchSpec]):
    def __init__(self, spawn_id: SpawnId, events: list[HarnessEvent]) -> None:
        self._spawn_id = spawn_id
        self._events = events

    @property
    def state(self) -> ConnectionState:
        return "connected"

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.CODEX

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def capabilities(self) -> ConnectionCapabilities:
        return ConnectionCapabilities(
            mid_turn_injection="queue",
            supports_steer=False,
            supports_cancel=False,
            runtime_model_switch=False,
            structured_reasoning=False,
        )

    @property
    def session_id(self) -> str | None:
        return None

    @property
    def subprocess_pid(self) -> int | None:
        return None

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = config, spec

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = reason, progress
        return StopResult()

    def health(self) -> bool:
        return True

    async def send_user_message(self, text: str) -> None:
        _ = text

    async def send_cancel(self) -> None:
        return

    async def events(self) -> AsyncIterator[HarnessEvent]:
        for event in self._events:
            yield event


class _FakeCoordinator:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")

    async def stop(self) -> None:
        self.calls.append("stop")

    def default_policy(self) -> DrainPolicy:
        self.calls.append("default_policy")
        return SingleTurnDrainPolicy()

    def set_policy(self, policy: DrainPolicy) -> None:
        self.calls.append("set_policy")

    def raw_terminal_frames_are_authoritative(self) -> bool:
        return True

    def next_timeout(self) -> float | None:
        self.calls.append("next_timeout")
        return None

    def wants_aux_wake(self) -> bool:
        return False

    async def wait_for_aux_wake(self) -> None:
        raise AssertionError("aux wake should not be armed")

    async def handle_aux_wake(self) -> DrainLoopDecision:
        self.calls.append("handle_aux_wake")
        return DrainLoopDecision()

    async def observe_event(self, event: HarnessEvent, transition: str | None) -> bool:
        self.calls.append(f"observe_event:{transition}")
        return False

    def note_event_persisted(self, event: HarnessEvent) -> DrainLoopDecision:
        self.calls.append("note_event_persisted")
        return DrainLoopDecision()

    async def handle_terminal_event(
        self,
        event: HarnessEvent,
        outcome: TerminalEventOutcome,
        action: DrainAction,
    ) -> DrainTerminalDecision:
        self.calls.append(f"handle_terminal_event:{outcome.status}:{action.terminate}")
        return DrainTerminalDecision(recorded_outcome=outcome)

    async def handle_timeout(self) -> DrainLoopDecision:
        self.calls.append("handle_timeout")
        return DrainLoopDecision()

    async def after_event(self) -> DrainLoopDecision:
        self.calls.append("after_event")
        return DrainLoopDecision()

    def handle_close(self, *, intentional_stop: bool) -> TerminalEventOutcome | None:
        self.calls.append(f"handle_close:{intentional_stop}")
        return None

    async def handle_stream_exit(
        self,
        recorded_outcome: TerminalEventOutcome | None,
    ) -> DrainExitDecision:
        self.calls.append("handle_stream_exit")
        return DrainExitDecision(recorded_outcome=recorded_outcome)

    def after_finalized(
        self,
        *,
        connection_session_id: str | None,
        outcome: DrainOutcome,
    ) -> None:
        self.calls.append(f"after_finalized:{outcome.status}:{outcome.exit_code}:{outcome.error}")


@pytest.mark.asyncio
async def test_spawn_drain_loop_drives_coordinator_protocol() -> None:
    spawn_id = SpawnId("s-protocol")
    event = HarnessEvent(event_type="turn/completed", payload={}, harness_id="codex")
    receiver = _FakeConnection(spawn_id, [event])
    coordinator = _FakeCoordinator()

    completion_future: asyncio.Future[DrainOutcome] = asyncio.Future()
    sessions = {
        spawn_id: SpawnSession(
            connection=receiver,
            drain_task=asyncio.current_task() or asyncio.create_task(asyncio.sleep(0)),
            subscriber=None,
            control_server=cast("Any", None),
            started_monotonic=time.monotonic(),
            completion_future=completion_future,
        )
    }
    cleanup_calls: list[SpawnId] = []

    async def _cleanup_completed_session(
        cleanup_spawn_id: SpawnId,
        session: SpawnSession,
    ) -> None:
        _ = session
        cleanup_calls.append(cleanup_spawn_id)

    cleanup_tasks: set[asyncio.Task[None]] = set()
    loop = SpawnDrainLoop(
        sessions=sessions,
        history_writers={},
        observers=EventObserverRegistry(),
        cleanup_tasks=cleanup_tasks,
        cleanup_completed_session=_cleanup_completed_session,
        resolve_completion_future=lambda session, outcome: outcome,
        fan_out_event=lambda fanout_spawn_id, fanout_event: None,
        fan_out_turn_boundary=lambda fanout_spawn_id, outcome: asyncio.sleep(0),
    )

    await loop.run(
        spawn_id=spawn_id,
        receiver=receiver,
        coordinator=coordinator,
        drain_policy=None,
        tracer=None,
    )

    if cleanup_tasks:
        await asyncio.gather(*cleanup_tasks)

    assert "default_policy" in coordinator.calls
    assert "observe_event:idle" in coordinator.calls
    assert "handle_terminal_event:succeeded:True" in coordinator.calls
    assert "after_finalized:succeeded:0:None" in coordinator.calls
    assert cleanup_calls == [spawn_id]
