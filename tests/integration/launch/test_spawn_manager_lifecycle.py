# qa-validated: test-suite-redesign
"""SpawnManager lifecycle tests: completion, backpressure, and serialized control actions."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from meridian.lib.core.types import HarnessId, SpawnId, TransportId
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    RawHarnessEvent,
    StopResult,
)
from meridian.lib.harness.control_action import ControlActionCoordinator
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state.paths import resolve_runtime_paths
from meridian.lib.state.spawn_store import start_spawn
from meridian.lib.streaming import spawn_manager as spawn_manager_module
from meridian.lib.streaming.drain_coordinator import DrainPlan
from meridian.lib.streaming.drain_teardown import DEFAULT_DRAIN_SESSION_TEARDOWN
from meridian.lib.streaming.spawn_manager import DrainOutcome, SpawnManager, SpawnSession
from meridian.lib.streaming.types import InjectResult
from meridian.lib.telemetry import init_telemetry
from tests.support.async_determinism import assert_still_pending
from tests.support.fakes import RecordingTelemetrySink, wait_for_telemetry


def _build_config(
    spawn_id: SpawnId,
    project_root: Path,
    *,
    harness_id: HarnessId = HarnessId.CODEX,
) -> ConnectionConfig:
    pi_session_role = "spawned" if harness_id is HarnessId.PI else None
    return ConnectionConfig(
        spawn_id=spawn_id,
        harness_id=harness_id,
        prompt="hello",
        control_root=project_root,
        env_overrides={},
        pi_session_role=pi_session_role,
    )


def _build_spec() -> ResolvedLaunchSpec:
    return ResolvedLaunchSpec(
        prompt="hello",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )


def _read_output_event_types(runtime_root: Path, spawn_id: SpawnId) -> list[str]:
    output_path = runtime_root / "spawns" / str(spawn_id) / "history.jsonl"
    if not output_path.exists():
        return []
    events: list[str] = []
    for line in output_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = cast("dict[str, object]", json.loads(line))
        event_type = payload.get("event_type")
        if isinstance(event_type, str):
            events.append(event_type)
    return events


@pytest.mark.asyncio
async def test_start_spawn_cancellation_during_registration_stops_connection(
    tmp_path: Path,
) -> None:
    """Cancellation cannot strand a connection before manager registration."""

    connection_stopped = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    control_start_entered = asyncio.Event()
    release_control_start = asyncio.Event()

    class FakeConnection:
        @property
        def harness_id(self) -> HarnessId:
            return HarnessId.CODEX

        @property
        def resident_backend(self) -> None:
            return None

        async def stop(
            self,
            *,
            reason: str | None = None,
            progress: Any = None,
        ) -> StopResult:
            _ = reason, progress
            cleanup_started.set()
            await release_cleanup.wait()
            connection_stopped.set()
            return StopResult()

    connection = FakeConnection()

    async def start_connection(
        _config: ConnectionConfig,
        _spec: ResolvedLaunchSpec,
    ) -> Any:
        return connection

    class BlockingControlServer:
        async def start(self) -> None:
            control_start_entered.set()
            await release_control_start.wait()

        async def stop(self) -> None:
            return None

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        start_connection=start_connection,
        control_server_factory=lambda *_args: cast("Any", BlockingControlServer()),
    )
    spawn_id = SpawnId("p-registration-cancel")
    start_task = asyncio.create_task(
        manager.start_spawn(_build_config(spawn_id, tmp_path), _build_spec())
    )

    await control_start_entered.wait()
    start_task.cancel()
    await cleanup_started.wait()
    start_task.cancel()
    await asyncio.sleep(0)
    assert not start_task.done()
    assert not connection_stopped.is_set()

    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert connection_stopped.is_set()
    assert manager.get_connection(spawn_id) is None


@pytest.mark.asyncio
async def test_wait_for_completion_survives_cleanup_without_private_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path
    runtime_root = resolve_runtime_paths(project_root).root_dir
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    class FakeControlSocketServer:
        def __init__(self, spawn_id: SpawnId, socket_path: Path, manager: SpawnManager) -> None:
            _ = spawn_id, manager
            self.socket_path = socket_path

        async def start(self) -> None:
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        async def stop(self) -> None:
            return None

    class FakeConnection:
        def __init__(self) -> None:
            self._spawn_id = SpawnId("")
            self.state = "created"
            self.capabilities = ConnectionCapabilities(
                mid_turn_injection="queue",
                supports_steer=True,
                supports_cancel=True,
                runtime_model_switch=False,
                structured_reasoning=False,
            )

        @property
        def harness_id(self) -> HarnessId:
            return HarnessId.CODEX

        @property
        def spawn_id(self) -> SpawnId:
            return self._spawn_id

        @property
        def subprocess_pid(self) -> int | None:
            return 7373

        @property
        def primary_event_scope(self) -> None:
            return None

        @property
        def resident_backend(self) -> None:
            return None

        async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
            _ = spec
            self._spawn_id = config.spawn_id
            self.state = "connected"

        async def stop(
            self,
            *,
            reason: str | None = None,
            progress: Any = None,
        ) -> StopResult:
            _ = reason, progress
            cleanup_started.set()
            await release_cleanup.wait()
            self.state = "stopped"
            return StopResult()

        def health(self) -> bool:
            return True

        async def send_user_message(self, text: str) -> None:
            _ = text

        async def send_cancel(self) -> None:
            return None

        async def events(self):  # type: ignore[no-untyped-def]
            yield RawHarnessEvent(
                event_type="item.completed",
                harness_id="codex",
                payload={
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "done"},
                },
            )

    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: FakeConnection,
    )

    spawn_id = start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.3-codex",
        agent="coder",
        harness="codex",
        kind="streaming",
        prompt="hello",
        launch_mode="foreground",
        status="running",
    )
    manager = SpawnManager(runtime_root=runtime_root, project_root=project_root)
    await manager.start_spawn(_build_config(spawn_id, project_root), _build_spec())

    try:
        await cleanup_started.wait()
        completion_before_cleanup_release = await manager.wait_for_completion(spawn_id)
        assert completion_before_cleanup_release is not None
        assert completion_before_cleanup_release.status == "failed"
        assert completion_before_cleanup_release.exit_code == 1
        assert completion_before_cleanup_release.error == "connection_closed_without_terminal_event"

        # Session cleanup removes live connection before cleanup fully drains.
        assert manager.get_connection(spawn_id) is None

        inject_result = await manager.inject(spawn_id, "late message")
        assert inject_result == InjectResult(
            success=False,
            error=f"Spawn {spawn_id} is not active",
        )
        assert "item.completed" in _read_output_event_types(runtime_root, spawn_id)

        release_cleanup.set()
        await asyncio.sleep(0)
        completion_after_cleanup_release = await manager.wait_for_completion(spawn_id)
        assert completion_after_cleanup_release == completion_before_cleanup_release
    finally:
        release_cleanup.set()
        await asyncio.sleep(0)
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_backpressure_drop_emits_runtime_telemetry(tmp_path: Path) -> None:
    sink = RecordingTelemetrySink()
    init_telemetry(sink=sink)
    project_root = tmp_path
    runtime_root = resolve_runtime_paths(project_root).root_dir
    spawn_id = SpawnId("p-drop")
    manager = SpawnManager(runtime_root=runtime_root, project_root=project_root)

    class FakeConnection:
        @property
        def harness_id(self) -> HarnessId:
            return HarnessId.CODEX

        @property
        def resident_backend(self) -> None:
            return None

        async def stop(
            self,
            *,
            reason: str | None = None,
            progress: Any = None,
        ) -> StopResult:
            _ = reason, progress
            return StopResult()

    class FakeControlServer:
        async def stop(self) -> None:
            return None

    completion_future: asyncio.Future = asyncio.get_running_loop().create_future()
    manager._sessions[spawn_id] = SpawnSession(
        connection=cast("Any", FakeConnection()),
        drain_task=asyncio.create_task(asyncio.sleep(0)),
        subscriber=asyncio.Queue(maxsize=1),
        control_server=cast("Any", FakeControlServer()),
        started_monotonic=time.monotonic(),
        completion_future=completion_future,
        raw_terminal_frames_authoritative=True,
        teardown=DEFAULT_DRAIN_SESSION_TEARDOWN,
        drain_plan=DrainPlan(),
        control_actions=ControlActionCoordinator(
            spawn_id=spawn_id,
            spawn_dir=manager._spawn_dir(spawn_id),
        ),
    )

    first = RawHarnessEvent(event_type="first", harness_id="codex", payload={})
    second = RawHarnessEvent(event_type="second", harness_id="codex", payload={})
    manager._fan_out_event(spawn_id, first)
    manager._fan_out_event(spawn_id, second)

    wait_for_telemetry(
        lambda: any(event.event == "runtime.stream_event_dropped" for event in sink.events)
    )
    event = next(event for event in sink.events if event.event == "runtime.stream_event_dropped")
    assert event.scope == "streaming.spawn_manager"
    assert event.severity == "warning"
    assert event.ids == {"spawn_id": "p-drop"}
    assert event.data["event_type"] == "second"
    assert event.data["error"]["type"] == "QueueFullBackpressure"


@pytest.mark.asyncio
async def test_stop_spawn_reaps_recorded_scope_when_connection_stop_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path
    runtime_root = resolve_runtime_paths(project_root).root_dir
    spawn_id = start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.3-codex",
        agent="coder",
        harness="codex",
        kind="streaming",
        prompt="hello",
        launch_mode="foreground",
        status="running",
    )
    reaped: list[tuple[str, str]] = []

    def _record_reap(runtime_root_arg: Path, record: Any, *, reason: str) -> list[Any]:
        assert runtime_root_arg == runtime_root
        reaped.append((record.id, reason))
        return []

    monkeypatch.setattr(
        spawn_manager_module,
        "terminate_recorded_spawn_scope",
        _record_reap,
        raising=False,
    )

    class StopRaisesConnection:
        @property
        def harness_id(self) -> HarnessId:
            return HarnessId.CODEX

        @property
        def resident_backend(self) -> None:
            return None

        async def stop(
            self,
            *,
            reason: str | None = None,
            progress: Any = None,
        ) -> StopResult:
            _ = reason, progress
            raise RuntimeError("transport cleanup failed")

    class FakeControlServer:
        async def stop(self) -> None:
            return None

    completion_future: asyncio.Future = asyncio.get_running_loop().create_future()
    manager = SpawnManager(runtime_root=runtime_root, project_root=project_root)
    manager._sessions[spawn_id] = SpawnSession(
        connection=cast("Any", StopRaisesConnection()),
        drain_task=asyncio.create_task(asyncio.sleep(0)),
        subscriber=asyncio.Queue(maxsize=1),
        control_server=cast("Any", FakeControlServer()),
        started_monotonic=time.monotonic(),
        completion_future=completion_future,
        raw_terminal_frames_authoritative=True,
        teardown=DEFAULT_DRAIN_SESSION_TEARDOWN,
        drain_plan=DrainPlan(),
        control_actions=ControlActionCoordinator(
            spawn_id=spawn_id,
            spawn_dir=manager._spawn_dir(spawn_id),
        ),
    )

    outcome = await manager.stop_spawn(spawn_id)

    assert outcome is not None
    assert reaped == [(str(spawn_id), "stop_spawn")]


@pytest.mark.asyncio
async def test_stop_awaits_only_its_cleanup_and_shutdown_drains_the_rest(
    tmp_path: Path,
) -> None:
    manager = SpawnManager(runtime_root=tmp_path, project_root=tmp_path)
    own_spawn = SpawnId("p-own-cleanup")
    other_spawn = SpawnId("p-other-cleanup")
    own_done = asyncio.Event()
    release_other = asyncio.Event()
    other_done = asyncio.Event()

    async def _own_cleanup() -> None:
        own_done.set()

    async def _other_cleanup() -> None:
        await release_other.wait()
        other_done.set()

    completion: asyncio.Future[DrainOutcome] = asyncio.get_running_loop().create_future()
    outcome = DrainOutcome(status="succeeded", exit_code=0)
    completion.set_result(outcome)
    manager._sessions[own_spawn] = cast(
        "Any",
        SimpleNamespace(terminal_published=True, completion_future=completion),
    )
    manager._cleanup_tasks[own_spawn] = asyncio.create_task(_own_cleanup())
    manager._cleanup_tasks[other_spawn] = asyncio.create_task(_other_cleanup())

    assert await manager.stop_spawn(own_spawn) is outcome
    assert own_done.is_set()
    assert not other_done.is_set()

    release_other.set()
    await manager.shutdown()
    assert other_done.is_set()


@pytest.mark.asyncio
async def test_spawn_manager_serializes_control_actions_and_persists_transitions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path
    runtime_root = resolve_runtime_paths(project_root).root_dir

    class FakeControlSocketServer:
        def __init__(self, spawn_id: SpawnId, socket_path: Path, manager: SpawnManager) -> None:
            _ = spawn_id, manager
            self.socket_path = socket_path

        async def start(self) -> None:
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        async def stop(self) -> None:
            return None

    class FakeConnection:
        def __init__(self) -> None:
            self._spawn_id = SpawnId("")
            self.state = "created"
            self.inject_started = asyncio.Event()
            self.allow_inject_send = asyncio.Event()
            self.call_order: list[str] = []
            self.capabilities = ConnectionCapabilities(
                mid_turn_injection="interrupt_restart",
                supports_steer=True,
                supports_cancel=True,
                runtime_model_switch=False,
                structured_reasoning=False,
            )

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

        @property
        def primary_event_scope(self) -> None:
            return None

        @property
        def resident_backend(self) -> None:
            return None

        async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
            _ = spec
            self._spawn_id = config.spawn_id
            self.state = "connected"

        async def stop(
            self,
            *,
            reason: str | None = None,
            progress: Any = None,
        ) -> StopResult:
            _ = reason, progress
            self.state = "stopped"
            return StopResult()

        def health(self) -> bool:
            return self.state == "connected"

        async def send_user_message(self, text: str) -> None:
            _ = text
            self.call_order.append("inject:start")
            self.inject_started.set()
            await self.allow_inject_send.wait()
            self.call_order.append("inject:end")

        async def send_cancel(self) -> None:
            self.call_order.append("interrupt")

        async def respond_request(
            self,
            request_id: str,
            decision: str,
            payload: dict[str, object] | None = None,
        ) -> None:
            _ = payload
            self.call_order.append(f"approve:{request_id}:{decision}")

        async def respond_user_input(self, request_id: str, answers: dict[str, object]) -> None:
            _ = answers
            self.call_order.append(f"input:{request_id}")

        async def events(self):  # type: ignore[no-untyped-def]
            while self.state != "stopped":
                await asyncio.sleep(0.01)
                if False:
                    yield RawHarnessEvent(event_type="noop", payload={}, harness_id="codex")

    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: FakeConnection,
    )

    spawn_id = start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.3-codex",
        agent="coder",
        harness="codex",
        kind="streaming",
        prompt="hello",
        launch_mode="foreground",
        status="running",
    )
    manager = SpawnManager(runtime_root=runtime_root, project_root=project_root)
    connection = cast(
        "Any",
        await manager.start_spawn(_build_config(spawn_id, project_root), _build_spec()),
    )

    try:
        inject_task = asyncio.create_task(manager.inject(spawn_id, "hello", source="test"))
        await connection.inject_started.wait()

        interrupt_task = asyncio.create_task(manager.interrupt(spawn_id, source="test"))

        await assert_still_pending(interrupt_task)

        connection.allow_inject_send.set()
        inject_result = await inject_task
        await interrupt_task
        assert inject_result.success is True
        assert inject_result.inbound_seq == 0

        control_actions_path = runtime_root / "spawns" / str(spawn_id) / "control_actions.jsonl"
        assert control_actions_path.exists()
        records = [
            cast("dict[str, object]", json.loads(line))
            for line in control_actions_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        action_statuses: dict[str, list[str]] = {}
        inject_ack_index: int | None = None
        interrupt_requested_index: int | None = None
        for index, record in enumerate(records):
            action_id = cast("str", record["action_id"])
            action = cast("str", record["action"])
            status = cast("str", record["status"])
            action_statuses.setdefault(action_id, []).append(status)
            if action == "inject" and status == "acknowledged":
                inject_ack_index = index
            if action == "interrupt" and status == "requested":
                interrupt_requested_index = index
        assert all(
            statuses == ["requested", "sent", "acknowledged"]
            for statuses in action_statuses.values()
        )
        recorded_actions = {cast("str", record["action"]) for record in records}
        assert recorded_actions == {"inject", "interrupt"}
        assert inject_ack_index is not None
        assert interrupt_requested_index is not None
        assert inject_ack_index < interrupt_requested_index
    finally:
        await manager.stop_spawn(spawn_id)
