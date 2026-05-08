from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    HarnessEvent,
)
from meridian.lib.harness.launch_spec import CodexLaunchSpec, ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state.paths import resolve_runtime_paths
from meridian.lib.streaming import event_observers
from meridian.lib.streaming import spawn_manager as spawn_manager_module
from meridian.lib.streaming.event_observers import EventObserverRegistry
from meridian.lib.streaming.spawn_manager import SpawnManager


def _event(event_type: str) -> HarnessEvent:
    return HarnessEvent(event_type=event_type, harness_id="codex", payload={})


def _config(spawn_id: SpawnId, project_root: Path) -> ConnectionConfig:
    return ConnectionConfig(
        spawn_id=spawn_id,
        harness_id=HarnessId.CODEX,
        prompt="hello",
        project_root=project_root,
        env_overrides={},
    )


def _spec() -> CodexLaunchSpec:
    return CodexLaunchSpec(
        prompt="hello",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )


class FakeControlSocketServer:
    def __init__(self, spawn_id: SpawnId, socket_path: Path, manager: SpawnManager) -> None:
        _ = spawn_id, manager
        self.socket_path = socket_path

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

    async def stop(self) -> None:
        return None

    @property
    def endpoint(self) -> str:
        return f"unix://{self.socket_path}"


class ScriptedConnection:
    def __init__(
        self,
        spawn_id: SpawnId,
        events: list[HarnessEvent],
        release: asyncio.Event,
    ) -> None:
        self._spawn_id = spawn_id
        self._events = events
        self._release = release
        self._state = "connected"
        self.capabilities = ConnectionCapabilities(
            mid_turn_injection="queue",
            supports_steer=True,
            supports_cancel=True,
            runtime_model_switch=False,
            structured_reasoning=False,
        )

    @property
    def state(self) -> str:
        return self._state

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.CODEX

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def session_id(self) -> str | None:
        return None

    @property
    def subprocess_pid(self) -> int | None:
        return None

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = config, spec

    async def stop(self) -> None:
        self._state = "stopped"

    def health(self) -> bool:
        return True

    async def send_user_message(self, text: str) -> None:
        _ = text

    async def send_cancel(self) -> None:
        return None

    async def events(self) -> AsyncIterator[HarnessEvent]:
        await self._release.wait()
        for event in self._events:
            yield event


async def _start_scripted_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    spawn_id: SpawnId,
    events: list[HarnessEvent],
    release: asyncio.Event,
) -> SpawnManager:
    project_root = tmp_path
    runtime_root = resolve_runtime_paths(project_root).root_dir
    connection = ScriptedConnection(spawn_id, events, release)

    async def fake_dispatch_start(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> ScriptedConnection:
        await connection.start(config, spec)
        return connection

    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", FakeControlSocketServer)
    monkeypatch.setattr(spawn_manager_module, "dispatch_start", fake_dispatch_start)

    manager = SpawnManager(runtime_root=runtime_root, project_root=project_root)
    await manager.start_spawn(_config(spawn_id, project_root), _spec())
    return manager


@pytest.mark.asyncio
async def test_slow_observer_does_not_block_drain_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_id = SpawnId("s-slow")
    release_events = asyncio.Event()
    release_observer = asyncio.Event()
    observer_entered = asyncio.Event()

    class SlowObserver:
        async def on_event(self, spawn_id: SpawnId, event: HarnessEvent) -> None:
            _ = spawn_id, event
            observer_entered.set()
            await release_observer.wait()

        async def on_complete(self, spawn_id: SpawnId) -> None:
            _ = spawn_id

    manager = await _start_scripted_manager(
        tmp_path,
        monkeypatch,
        spawn_id=spawn_id,
        events=[_event("turn/started"), _event("item.completed"), _event("turn/completed")],
        release=release_events,
    )
    manager.register_observer(spawn_id, SlowObserver())
    subscriber = manager.subscribe(spawn_id)
    assert subscriber is not None

    release_events.set()
    await asyncio.wait_for(observer_entered.wait(), timeout=1.0)

    received = [
        (await asyncio.wait_for(subscriber.get(), timeout=1.0)).event_type,
        (await asyncio.wait_for(subscriber.get(), timeout=1.0)).event_type,
        (await asyncio.wait_for(subscriber.get(), timeout=1.0)).event_type,
    ]

    assert received == ["turn/started", "item.completed", "turn/completed"]

    release_observer.set()
    await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_failing_observer_does_not_block_other_observers_or_subscriber(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    spawn_id = SpawnId("s-failing-manager")
    release_events = asyncio.Event()
    healthy_events: list[str] = []

    class FailingObserver:
        async def on_event(self, spawn_id: SpawnId, event: HarnessEvent) -> None:
            _ = spawn_id, event
            raise RuntimeError("observer exploded")

        async def on_complete(self, spawn_id: SpawnId) -> None:
            _ = spawn_id

    class HealthyObserver:
        async def on_event(self, spawn_id: SpawnId, event: HarnessEvent) -> None:
            _ = spawn_id
            healthy_events.append(event.event_type)

        async def on_complete(self, spawn_id: SpawnId) -> None:
            _ = spawn_id

    manager = await _start_scripted_manager(
        tmp_path,
        monkeypatch,
        spawn_id=spawn_id,
        events=[_event("turn/started"), _event("turn/completed")],
        release=release_events,
    )
    manager.register_observer(spawn_id, FailingObserver())
    manager.register_observer(spawn_id, HealthyObserver())
    subscriber = manager.subscribe(spawn_id)
    assert subscriber is not None

    with caplog.at_level(logging.ERROR):
        release_events.set()
        first = await asyncio.wait_for(subscriber.get(), timeout=1.0)
        second = await asyncio.wait_for(subscriber.get(), timeout=1.0)
        await manager.stop_spawn(spawn_id)

    assert [first.event_type, second.event_type] == ["turn/started", "turn/completed"]
    assert healthy_events == ["turn/started", "turn/completed"]
    assert "Observer failed for spawn s-failing-manager" in caplog.text


@pytest.mark.asyncio
async def test_on_event_callback_remains_compatible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_id = SpawnId("s-callback")
    release_events = asyncio.Event()
    callback_events: list[str] = []
    callback_done = asyncio.Event()
    project_root = tmp_path
    runtime_root = resolve_runtime_paths(project_root).root_dir
    connection = ScriptedConnection(
        spawn_id,
        [_event("turn/started"), _event("turn/completed")],
        release_events,
    )

    async def fake_dispatch_start(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> ScriptedConnection:
        await connection.start(config, spec)
        return connection

    async def on_event(event: HarnessEvent) -> None:
        callback_events.append(event.event_type)
        if event.event_type == "turn/completed":
            callback_done.set()

    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", FakeControlSocketServer)
    monkeypatch.setattr(spawn_manager_module, "dispatch_start", fake_dispatch_start)

    manager = SpawnManager(runtime_root=runtime_root, project_root=project_root)
    await manager.start_spawn(_config(spawn_id, project_root), _spec(), on_event=on_event)

    release_events.set()
    await asyncio.wait_for(callback_done.wait(), timeout=1.0)
    await manager.stop_spawn(spawn_id)

    assert callback_events == ["turn/started", "turn/completed"]


@pytest.mark.asyncio
async def test_start_spawn_uses_injected_ports(tmp_path: Path) -> None:
    spawn_id = SpawnId("s-injected-ports")
    project_root = tmp_path
    runtime_root = resolve_runtime_paths(project_root).root_dir
    release_events = asyncio.Event()
    captured: dict[str, object] = {}

    connection = ScriptedConnection(
        spawn_id,
        [_event("turn/completed")],
        release_events,
    )

    async def start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> ScriptedConnection:
        captured["start_spawn_id"] = str(config.spawn_id)
        await connection.start(config, spec)
        return connection

    class RecordingControlSocketServer(FakeControlSocketServer):
        def __init__(self, spawn_id: SpawnId, socket_path: Path, manager: SpawnManager) -> None:
            super().__init__(spawn_id, socket_path, manager)
            captured["control_spawn_id"] = str(spawn_id)
            captured["control_socket_path"] = str(socket_path)

    manager = SpawnManager(
        runtime_root=runtime_root,
        project_root=project_root,
        start_connection=start_connection,
        control_server_factory=RecordingControlSocketServer,
    )
    await manager.start_spawn(_config(spawn_id, project_root), _spec())
    subscriber = manager.subscribe(spawn_id)
    assert subscriber is not None

    release_events.set()
    first = await asyncio.wait_for(subscriber.get(), timeout=1.0)
    assert first is not None
    assert first.event_type == "turn/completed"
    expected_endpoint = f"unix://{runtime_root / 'spawns' / str(spawn_id) / 'control.sock'}"
    assert manager.control_endpoint(spawn_id) == expected_endpoint

    assert captured["start_spawn_id"] == str(spawn_id)
    assert captured["control_spawn_id"] == str(spawn_id)
    assert str(runtime_root / "spawns" / str(spawn_id) / "control.sock") == captured[
        "control_socket_path"
    ]
    await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_observer_shutdown_times_out_and_cancels_hung_observer(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    spawn_id = SpawnId("s-hung-observer")
    registry = EventObserverRegistry()
    observer_entered = asyncio.Event()
    observer_cancelled = asyncio.Event()

    class HungObserver:
        async def on_event(self, spawn_id: SpawnId, event: HarnessEvent) -> None:
            _ = spawn_id, event
            observer_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                observer_cancelled.set()
                raise

        async def on_complete(self, spawn_id: SpawnId) -> None:
            _ = spawn_id

    monkeypatch.setattr(event_observers, "_OBSERVER_SHUTDOWN_TIMEOUT_SECONDS", 0.01)
    registry.register(spawn_id, HungObserver())
    registry.dispatch(spawn_id, _event("turn/started"))
    await asyncio.wait_for(observer_entered.wait(), timeout=1.0)

    with caplog.at_level(logging.WARNING):
        await registry.shutdown(spawn_id)

    await asyncio.wait_for(observer_cancelled.wait(), timeout=1.0)
    assert "Observer shutdown timed out for spawn s-hung-observer" in caplog.text


@pytest.mark.asyncio
async def test_persist_observer_enqueue_subscriber_fanout_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_id = SpawnId("s-order")
    release_events = asyncio.Event()
    calls: list[str] = []

    class RecordingWriter:
        last_seq = -1

        def __init__(self, path: Path) -> None:
            _ = path

        def write(self, event: HarnessEvent) -> object:
            calls.append(f"persist:{event.event_type}")
            self.last_seq += 1

            class Result:
                success = True
                error: str | None = None

            return Result()

    class RecordingRegistry:
        def register(self, spawn_id: SpawnId, observer: object) -> None:
            _ = spawn_id, observer

        def dispatch(self, spawn_id: SpawnId, event: HarnessEvent) -> None:
            _ = spawn_id
            calls.append(f"observer:{event.event_type}")

        def complete(self, spawn_id: SpawnId) -> None:
            _ = spawn_id

        async def shutdown(self, spawn_id: SpawnId) -> None:
            _ = spawn_id

    manager = await _start_scripted_manager(
        tmp_path,
        monkeypatch,
        spawn_id=spawn_id,
        events=[_event("turn/started"), _event("turn/completed")],
        release=release_events,
    )
    manager._history_writers[spawn_id] = RecordingWriter(Path("unused"))
    manager._observers = RecordingRegistry()

    original_fan_out = manager._fan_out_event

    def recording_fan_out(spawn_id: SpawnId, event: HarnessEvent | None) -> None:
        if event is not None:
            calls.append(f"fanout:{event.event_type}")
        original_fan_out(spawn_id, event)

    monkeypatch.setattr(manager, "_fan_out_event", recording_fan_out)

    subscriber = manager.subscribe(spawn_id)
    assert subscriber is not None
    release_events.set()
    await asyncio.wait_for(subscriber.get(), timeout=1.0)
    await manager.stop_spawn(spawn_id)

    assert calls[:3] == [
        "persist:turn/started",
        "observer:turn/started",
        "fanout:turn/started",
    ]
