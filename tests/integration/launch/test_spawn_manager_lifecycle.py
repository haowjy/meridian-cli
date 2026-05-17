# qa-validated: test-suite-redesign
"""SpawnManager lifecycle tests: completion, backpressure, and serialized control actions."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, cast

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    HarnessEvent,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state.paths import resolve_runtime_paths
from meridian.lib.state.spawn_store import start_spawn
from meridian.lib.streaming import spawn_manager as spawn_manager_module
from meridian.lib.streaming.spawn_manager import SpawnManager, SpawnSession
from meridian.lib.streaming.types import InjectResult
from meridian.lib.telemetry import init_telemetry
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

        async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
            _ = spec
            self._spawn_id = config.spawn_id
            self.state = "connected"

        async def stop(self) -> None:
            cleanup_started.set()
            await release_cleanup.wait()
            self.state = "stopped"

        def health(self) -> bool:
            return True

        async def send_user_message(self, text: str) -> None:
            _ = text

        async def send_cancel(self) -> None:
            return None

        async def events(self):  # type: ignore[no-untyped-def]
            yield HarnessEvent(
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
        lambda _harness_id: FakeConnection,
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
        await asyncio.wait_for(cleanup_started.wait(), timeout=1.0)
        completion_before_cleanup_release = await asyncio.wait_for(
            manager.wait_for_completion(spawn_id), timeout=1.0
        )
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
        completion_after_cleanup_release = await asyncio.wait_for(
            manager.wait_for_completion(spawn_id), timeout=1.0
        )
        assert completion_after_cleanup_release == completion_before_cleanup_release
    finally:
        release_cleanup.set()
        await asyncio.sleep(0)
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_pi_terminal_waits_for_extension_subspawn_drain_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path
    runtime_root = resolve_runtime_paths(project_root).root_dir
    allow_child_drain = asyncio.Event()

    class FakeControlSocketServer:
        def __init__(self, spawn_id: SpawnId, socket_path: Path, manager: SpawnManager) -> None:
            _ = spawn_id, manager
            self.socket_path = socket_path

        async def start(self) -> None:
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        async def stop(self) -> None:
            return None

    class FakePiConnection:
        def __init__(self) -> None:
            self._spawn_id = SpawnId("")
            self.state = "created"
            self.capabilities = ConnectionCapabilities(
                mid_turn_injection="queue",
                supports_steer=False,
                supports_cancel=True,
                runtime_model_switch=False,
                structured_reasoning=True,
            )

        @property
        def harness_id(self) -> HarnessId:
            return HarnessId.PI

        @property
        def spawn_id(self) -> SpawnId:
            return self._spawn_id

        @property
        def subprocess_pid(self) -> int | None:
            return 7373

        async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
            _ = spec
            self._spawn_id = config.spawn_id
            self.state = "connected"

        async def stop(self) -> None:
            self.state = "stopped"

        def health(self) -> bool:
            return self.state == "connected"

        async def send_user_message(self, text: str) -> None:
            _ = text

        async def send_cancel(self) -> None:
            return None

        async def events(self):  # type: ignore[no-untyped-def]
            yield HarnessEvent(
                event_type="session",
                harness_id="pi",
                payload={"type": "session", "id": "ses-1"},
            )
            yield HarnessEvent(
                event_type="meridian.subspawn.start",
                harness_id="pi",
                payload={
                    "type": "meridian.subspawn.start",
                    "schema_version": 1,
                    "subspawn_id": "child-1",
                    "wait_policy": "tracked",
                },
            )
            yield HarnessEvent(
                event_type="agent_end",
                harness_id="pi",
                payload={
                    "type": "agent_end",
                    "messages": [{"role": "assistant", "stopReason": "stop"}],
                },
            )
            await allow_child_drain.wait()
            yield HarnessEvent(
                event_type="meridian.subspawn.end",
                harness_id="pi",
                payload={
                    "type": "meridian.subspawn.end",
                    "schema_version": 1,
                    "subspawn_id": "child-1",
                    "wait_policy": "tracked",
                },
            )

    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id: FakePiConnection,
    )

    spawn_id = start_spawn(
        runtime_root,
        chat_id="c-pi-child-drain",
        model="openai-codex/gpt-5.4-mini",
        agent="coder",
        harness="pi",
        kind="streaming",
        prompt="hello",
        launch_mode="foreground",
        status="running",
    )
    manager = SpawnManager(
        runtime_root=runtime_root,
        project_root=project_root,
        pi_quiescence_idle_grace_secs=0.01,
    )
    await manager.start_spawn(
        _build_config(spawn_id, project_root, harness_id=HarnessId.PI),
        _build_spec(),
    )

    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(manager.wait_for_completion(spawn_id)),
                timeout=0.1,
            )

        allow_child_drain.set()
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=1.0)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert outcome.exit_code == 0
        assert outcome.error is None
    finally:
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

        async def stop(self) -> None:
            return None

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
    )

    first = HarnessEvent(event_type="first", harness_id="codex", payload={})
    second = HarnessEvent(event_type="second", harness_id="codex", payload={})
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

        async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
            _ = spec
            self._spawn_id = config.spawn_id
            self.state = "connected"

        async def stop(self) -> None:
            self.state = "stopped"

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
                    yield HarnessEvent(event_type="noop", payload={}, harness_id="codex")

    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id: FakeConnection,
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
        await asyncio.wait_for(connection.inject_started.wait(), timeout=1.0)

        interrupt_task = asyncio.create_task(manager.interrupt(spawn_id, source="test"))
        await asyncio.sleep(0.05)
        assert not interrupt_task.done()

        connection.allow_inject_send.set()
        inject_result = await asyncio.wait_for(inject_task, timeout=1.0)
        await asyncio.wait_for(interrupt_task, timeout=1.0)
        await manager.respond_request(
            spawn_id,
            request_id="r1",
            decision="accept",
            payload={"x": 1},
            source="test",
        )
        await manager.respond_user_input(
            spawn_id,
            request_id="u1",
            answers={"text": "Ada"},
            source="test",
        )

        assert inject_result.success is True
        assert inject_result.inbound_seq == 0
        assert connection.call_order[:3] == ["inject:start", "inject:end", "interrupt"]
        assert "approve:r1:accept" in connection.call_order
        assert "input:u1" in connection.call_order

        control_actions_path = runtime_root / "spawns" / str(spawn_id) / "control_actions.jsonl"
        assert control_actions_path.exists()
        records = [
            cast("dict[str, object]", json.loads(line))
            for line in control_actions_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        action_statuses: dict[str, list[str]] = {}
        for record in records:
            action_id = cast("str", record["action_id"])
            action_statuses.setdefault(action_id, []).append(cast("str", record["status"]))
        assert all(
            statuses == ["requested", "sent", "acknowledged"]
            for statuses in action_statuses.values()
        )
        recorded_actions = {cast("str", record["action"]) for record in records}
        assert recorded_actions == {"inject", "interrupt", "permission_reply", "user_input_reply"}
    finally:
        await manager.stop_spawn(spawn_id)
