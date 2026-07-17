from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

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
from meridian.lib.harness.connections.liveness import LivenessDecision
from meridian.lib.harness.connections.resident_backend import ResidentBackendControl
from meridian.lib.harness.semantics import (
    PrimaryEventScope,
    TerminalEventOutcome,
    opencode_primary_event_scope,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state import spawn_store
from meridian.lib.state.spawn_signals import write_spawn_signal
from meridian.lib.streaming.drain_policy import (
    TURN_BOUNDARY_EVENT_TYPE,
    DrainAction,
    DrainPolicy,
)
from meridian.lib.streaming.resident_drain import ResidentDrainCoordinator
from meridian.lib.streaming.spawn_manager import SpawnManager
from tests.support.fakes import FakeClock
from tests.support.pi import NoopControlServer


class FakeResidentBackendControl:
    def __init__(self) -> None:
        self.awaiting_done_values: list[bool] = []
        self.status: LivenessDecision = LivenessDecision.CONTINUE
        self.injected_messages: list[str] = []
        self.fail_inject: bool = False

    def health_status(self) -> LivenessDecision:
        return self.status

    def set_awaiting_done(self, awaiting: bool) -> None:
        self.awaiting_done_values.append(awaiting)

    async def begin_followup_turn(self, message: str) -> None:
        if self.fail_inject:
            raise RuntimeError("inject failed")
        self.injected_messages.append(message)


class FakeResidentConnection(HarnessConnection[ResolvedLaunchSpec]):
    def __init__(self, harness_id: HarnessId) -> None:
        self._harness_id = harness_id
        self._spawn_id = SpawnId("")
        self._state: ConnectionState = "created"
        self.resident_events: asyncio.Queue[HarnessEvent | None] = asyncio.Queue()
        self._resident_backend = FakeResidentBackendControl()
        self.stop_reasons: list[str | None] = []

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def harness_id(self) -> HarnessId:
        return self._harness_id

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def capabilities(self) -> ConnectionCapabilities:
        return ConnectionCapabilities(
            mid_turn_injection="queue",
            supports_steer=True,
            supports_cancel=True,
            runtime_model_switch=False,
            structured_reasoning=True,
        )

    @property
    def session_id(self) -> str | None:
        return "ses-resident"

    @property
    def primary_event_scope(self) -> PrimaryEventScope | None:
        if self._harness_id is HarnessId.OPENCODE:
            return opencode_primary_event_scope(self.session_id)
        return None

    @property
    def subprocess_pid(self) -> int | None:
        return 4242

    @property
    def fake_resident_backend(self) -> FakeResidentBackendControl:
        return self._resident_backend

    @property
    def resident_backend(self) -> ResidentBackendControl:
        return self._resident_backend

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = spec
        self._spawn_id = config.spawn_id
        self._state = "connected"

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = progress
        self.stop_reasons.append(reason)
        self._state = "stopped"
        return StopResult()

    def health(self) -> bool:
        return self._state == "connected"

    async def send_user_message(self, text: str) -> None:
        _ = text

    async def send_cancel(self) -> None:
        return None

    async def events(self) -> AsyncIterator[HarnessEvent]:
        while True:
            event = await self.resident_events.get()
            if event is None:
                return
            yield event

    def emit(self, event: HarnessEvent) -> None:
        self.resident_events.put_nowait(event)

    def close_stream(self) -> None:
        self.resident_events.put_nowait(None)

    def mark_failed(self) -> None:
        self._state = "failed"
        self._resident_backend.status = LivenessDecision.BACKEND_DEAD

    def mark_stalled(self) -> None:
        self._resident_backend.status = LivenessDecision.STREAM_STALLED


def descendant_cancellation_from_roots(
    project_root: Path,
    runtime_root: Path,
) -> Callable[[SpawnId], Awaitable[set[str]]]:
    """Build the same late-bound cancellation capability as the composition root."""

    async def _cancel_descendants(root_id: SpawnId) -> set[str]:
        from meridian.lib.bootstrap.services import (
            build_spawn_application_service_from_roots,
        )

        service = build_spawn_application_service_from_roots(project_root, runtime_root)
        return await service.cancel_descendants(root_id)

    return _cancel_descendants


async def awaiting_done_coordinator(
    tmp_path: Path,
    connection: HarnessConnection[Any],
) -> ResidentDrainCoordinator:
    resident_backend = connection.resident_backend
    assert resident_backend is not None
    coordinator = ResidentDrainCoordinator.for_connection(
        runtime_root=tmp_path,
        spawn_id=SpawnId("p1"),
        receiver=connection,
        resident_backend=resident_backend,
        deadline_seconds=30.0,
        poll_seconds=0.01,
        rearm_budget=None,
        cancel_descendants=descendant_cancellation_from_roots(tmp_path, tmp_path),
    )
    write_spawn_signal(tmp_path, "p1", "rearm")
    terminal_event = resident_event(connection.harness_id, "agent_end", {})
    await coordinator.observe_event(terminal_event, "idle")
    await coordinator.handle_terminal_event(
        terminal_event,
        TerminalEventOutcome(status="succeeded", exit_code=0),
        DrainAction(terminate=True, emit_turn_boundary=False),
    )
    return coordinator


def resident_event(
    harness_id: HarnessId,
    event_type: str,
    payload: dict[str, object],
) -> HarnessEvent:
    if harness_id is HarnessId.OPENCODE and "properties" not in payload:
        payload = {
            **payload,
            "type": event_type,
            "properties": {"sessionID": "ses-resident"},
        }
    return HarnessEvent(event_type=event_type, harness_id=harness_id.value, payload=payload)


async def next_turn_boundary(
    subscriber: asyncio.Queue[HarnessEvent | None],
) -> HarnessEvent:
    while True:
        event = await subscriber.get()
        assert event is not None
        if event.event_type == TURN_BOUNDARY_EVENT_TYPE:
            return event


def start_row(
    runtime_root: Path,
    spawn_id: str,
    harness: HarnessId,
    parent_id: str | None,
) -> None:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=SpawnId(spawn_id),
        chat_id=spawn_id,
        parent_id=parent_id,
        model="test-model",
        agent="test-agent",
        harness=harness.value,
        prompt="hello",
        status="running",
    )


async def start_manager(
    tmp_path: Path,
    connection: FakeResidentConnection,
    *,
    spawn_id: SpawnId,
    project_root: Path | None = None,
    resident_deadline_seconds: float = 30.0,
    resident_poll_seconds: float = 0.01,
    drain_policy: DrainPolicy | None = None,
) -> SpawnManager:
    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await connection.start(config, spec)
        return connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=project_root or tmp_path,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: NoopControlServer(),
    )
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=connection.harness_id,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            resident_deadline_seconds=resident_deadline_seconds,
            resident_poll_seconds=resident_poll_seconds,
        ),
        ResolvedLaunchSpec(
            harness=connection.harness_id.value,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
        drain_policy=drain_policy,
    )
    return manager


async def coordinator_with_clock(
    tmp_path: Path,
    connection: FakeResidentConnection,
    clock: FakeClock,
    *,
    deadline_seconds: float = 3300.0,
    poll_seconds: float = 5.0,
) -> ResidentDrainCoordinator:
    coordinator = ResidentDrainCoordinator.for_connection(
        runtime_root=tmp_path,
        spawn_id=SpawnId("p1"),
        receiver=connection,
        resident_backend=connection.resident_backend,
        deadline_seconds=deadline_seconds,
        poll_seconds=poll_seconds,
        rearm_budget=None,
        cancel_descendants=descendant_cancellation_from_roots(tmp_path, tmp_path),
    )
    write_spawn_signal(tmp_path, "p1", "rearm")
    terminal_event = resident_event(connection.harness_id, "agent_end", {})
    await coordinator.observe_event(terminal_event, "idle")
    await coordinator.handle_terminal_event(
        terminal_event,
        TerminalEventOutcome(status="succeeded", exit_code=0),
        DrainAction(terminate=True, emit_turn_boundary=False),
    )
    return coordinator
