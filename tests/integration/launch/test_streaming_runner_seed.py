# qa-validated: test-suite-redesign
"""Streaming runner seed persistence tests: Claude seed and OpenCode adapter seed port."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meridian.lib.core.domain import Spawn
from meridian.lib.core.types import HarnessId, ModelId, SpawnId, TransportId
from meridian.lib.harness.claude_utils import extract_session_id_from_args
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    RawHarnessEvent,
)
from meridian.lib.harness.launch_spec import ResolvedLaunchSpec
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch import context as launch_context_module
from meridian.lib.launch.request import SpawnRequest
from meridian.lib.launch.workspace_projection import ProjectionResult
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import LocalStore
from meridian.lib.state.paths import (
    resolve_project_runtime_root,
)
from meridian.lib.streaming import spawn_manager as spawn_manager_module
from tests.integration.launch.streaming_runner_support import (
    _execute_with_context,
    _FakeControlSocketServer,
)
from tests.support.fakes import FakeClock, FakeHeartbeat


class _ClaudeSeedPersistenceConnection:
    observed_start_session_id: str | None = None

    def __init__(self) -> None:
        self.state = "created"
        self._spawn_id = SpawnId("")
        self._project_root: Path | None = None
        self.capabilities = ConnectionCapabilities(
            mid_turn_injection="none",
            supports_steer=False,
            supports_cancel=False,
            runtime_model_switch=False,
            structured_reasoning=True,
        )

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.CLAUDE

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def session_id(self) -> str | None:
        return None

    @property
    def subprocess_pid(self) -> int | None:
        return 5252

    @property
    def primary_event_scope(self) -> None:
        return None

    def observe_event_semantics(self, semantics: object) -> None:
        _ = semantics

    @property
    def resident_backend(self) -> None:
        return None

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        self._spawn_id = config.spawn_id
        self._project_root = config.control_root
        runtime_root = resolve_project_runtime_root(config.control_root)
        row = spawn_store.get_spawn(runtime_root, config.spawn_id)
        assert row is not None
        self.__class__.observed_start_session_id = row.harness_session_id
        assert row.harness_session_id == extract_session_id_from_args(spec.extra_args)
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
        yield RawHarnessEvent(
            event_type="result",
            harness_id="claude",
            payload={"type": "result", "result": "seeded claude complete"},
        )


class _OpenCodeSeedPortConnection:
    observed_start_session_id: str | None = None

    def __init__(self) -> None:
        self.state = "created"
        self._spawn_id = SpawnId("")
        self.capabilities = ConnectionCapabilities(
            mid_turn_injection="none",
            supports_steer=False,
            supports_cancel=False,
            runtime_model_switch=False,
            structured_reasoning=True,
        )

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.OPENCODE

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def session_id(self) -> str | None:
        return None

    @property
    def subprocess_pid(self) -> int | None:
        return 6262

    @property
    def primary_event_scope(self) -> None:
        return None

    def observe_event_semantics(self, semantics: object) -> None:
        _ = semantics

    @property
    def resident_backend(self) -> None:
        return None

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = spec
        self._spawn_id = config.spawn_id
        runtime_root = resolve_project_runtime_root(config.control_root)
        row = spawn_store.get_spawn(runtime_root, config.spawn_id)
        assert row is not None
        self.__class__.observed_start_session_id = row.harness_session_id
        assert row.harness_session_id == "seeded-codex-session"
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
        yield RawHarnessEvent(
            event_type="result",
            harness_id="opencode",
            payload={"type": "result", "result": "seeded opencode complete"},
        )


class _OpenCodeConnectSessionConnection:
    observed_start_session_id: str | None = None

    def __init__(self) -> None:
        self.state = "created"
        self._spawn_id = SpawnId("")
        self._session_id = "connect-opencode-session"
        self.capabilities = ConnectionCapabilities(
            mid_turn_injection="queue",
            supports_steer=False,
            supports_cancel=False,
            runtime_model_switch=False,
            structured_reasoning=True,
        )

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.OPENCODE

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def subprocess_pid(self) -> int | None:
        return 7272

    @property
    def primary_event_scope(self) -> None:
        return None

    def observe_event_semantics(self, semantics: object) -> None:
        _ = semantics

    @property
    def resident_backend(self) -> None:
        return None

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = spec
        self._spawn_id = config.spawn_id
        if config.session_id_observer is not None:
            config.session_id_observer(self._session_id)
        runtime_root = resolve_project_runtime_root(config.control_root)
        row = spawn_store.get_spawn(runtime_root, config.spawn_id)
        assert row is not None
        self.__class__.observed_start_session_id = row.harness_session_id
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
        yield RawHarnessEvent(
            event_type="result",
            harness_id="opencode",
            payload={"type": "result", "result": "connect opencode complete"},
        )


def _build_claude_request() -> SpawnRequest:
    return SpawnRequest(
        model="claude-sonnet-4-6",
        harness=HarnessId.CLAUDE.value,
        prompt="hello",
    )


@pytest.mark.asyncio
async def test_execute_with_streaming_persists_claude_seed_before_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root(tmp_path)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    registry = HarnessRegistry.with_defaults()
    fake_clock = FakeClock(start=1_000.0)
    fake_heartbeat = FakeHeartbeat()
    fake_heartbeat.set_clock(fake_clock)
    _ClaudeSeedPersistenceConnection.observed_start_session_id = None

    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: _ClaudeSeedPersistenceConnection,
    )

    run = Spawn(
        spawn_id=SpawnId("r-claude-seed"),
        prompt="hello",
        model=ModelId("claude-sonnet-4-6"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-claude-seed",
        model=str(run.model),
        agent="",
        harness=HarnessId.CLAUDE.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=_build_claude_request(),
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=artifacts,
            registry=registry,
            clock=fake_clock,
            heartbeat_touch=fake_heartbeat.touch,
            heartbeat_interval_secs=0.001,
        ),
        timeout=15.0,
    )

    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert exit_code in (0, 1, 2)
    assert row is not None
    assert row.harness_session_id
    assert row.harness_session_id == _ClaudeSeedPersistenceConnection.observed_start_session_id


@pytest.mark.asyncio
async def test_execute_with_streaming_uses_adapter_seed_port_not_harness_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root(tmp_path)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    registry = HarnessRegistry.with_defaults()
    fake_clock = FakeClock(start=1_000.0)
    fake_heartbeat = FakeHeartbeat()
    fake_heartbeat.set_clock(fake_clock)
    _OpenCodeSeedPortConnection.observed_start_session_id = None

    opencode_adapter = registry.get_subprocess_harness(HarnessId.OPENCODE)
    monkeypatch.setattr(
        opencode_adapter,
        "derive_streaming_seeded_session_id",
        lambda **_kwargs: "seeded-opencode-session",
    )
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: _OpenCodeSeedPortConnection,
    )

    run = Spawn(
        spawn_id=SpawnId("r-opencode-seed-port"),
        prompt="hello",
        model=ModelId("gpt-5.4"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-opencode-seed",
        model=str(run.model),
        agent="",
        harness=HarnessId.OPENCODE.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=SpawnRequest(
                model="gpt-5.4",
                harness=HarnessId.OPENCODE.value,
                prompt="hello",
            ),
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=artifacts,
            registry=registry,
            clock=fake_clock,
            heartbeat_touch=fake_heartbeat.touch,
            heartbeat_interval_secs=0.001,
        ),
        timeout=15.0,
    )

    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert exit_code in (0, 1, 2)
    assert row is not None
    assert row.harness_session_id == "seeded-opencode-session"
    assert row.harness_session_id == _OpenCodeSeedPortConnection.observed_start_session_id


@pytest.mark.asyncio
async def test_execute_with_streaming_persists_opencode_session_id_at_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root(tmp_path)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    registry = HarnessRegistry.with_defaults()
    fake_clock = FakeClock(start=1_000.0)
    fake_heartbeat = FakeHeartbeat()
    fake_heartbeat.set_clock(fake_clock)
    _OpenCodeConnectSessionConnection.observed_start_session_id = None
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: _OpenCodeConnectSessionConnection,
    )

    run = Spawn(
        spawn_id=SpawnId("r-opencode-connect-session"),
        prompt="hello",
        model=ModelId("gpt-5.4"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-opencode-connect",
        model=str(run.model),
        agent="",
        harness=HarnessId.OPENCODE.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=SpawnRequest(
                model="gpt-5.4",
                harness=HarnessId.OPENCODE.value,
                prompt="hello",
            ),
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=artifacts,
            registry=registry,
            clock=fake_clock,
            heartbeat_touch=fake_heartbeat.touch,
            heartbeat_interval_secs=0.001,
        ),
        timeout=15.0,
    )

    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert exit_code in (0, 1, 2)
    assert row is not None
    assert row.harness_session_id == "connect-opencode-session"
    assert row.harness_session_id == _OpenCodeConnectSessionConnection.observed_start_session_id


@pytest.mark.asyncio
async def test_execute_with_streaming_persists_selected_task_cwd_on_projection_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root(tmp_path)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    registry = HarnessRegistry.with_defaults()
    fake_clock = FakeClock(start=1_000.0)
    fake_heartbeat = FakeHeartbeat()
    fake_heartbeat.set_clock(fake_clock)
    external_task_cwd = tmp_path.parent / f"{tmp_path.name}-outside-task"
    external_task_cwd.mkdir(parents=True, exist_ok=True)

    opencode_adapter = registry.get_subprocess_harness(HarnessId.OPENCODE)
    monkeypatch.setattr(
        opencode_adapter,
        "derive_streaming_seeded_session_id",
        lambda **_kwargs: "seeded-opencode-session",
    )
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: _OpenCodeSeedPortConnection,
    )
    monkeypatch.setattr(
        launch_context_module,
        "project_workspace_roots",
        lambda **_kwargs: ProjectionResult(applicability="unsupported:requires_config_generation"),
    )

    run = Spawn(
        spawn_id=SpawnId("r-opencode-task-cwd-fallback"),
        prompt="hello",
        model=ModelId("gpt-5.4"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-opencode-task-cwd-fallback",
        model=str(run.model),
        agent="",
        harness=HarnessId.OPENCODE.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=SpawnRequest(
                model="gpt-5.4",
                harness=HarnessId.OPENCODE.value,
                prompt="hello",
            ),
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=artifacts,
            registry=registry,
            execution_cwd=external_task_cwd,
            clock=fake_clock,
            heartbeat_touch=fake_heartbeat.touch,
            heartbeat_interval_secs=0.001,
        ),
        timeout=15.0,
    )

    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert exit_code in (0, 1, 2)
    assert row is not None
    assert row.control_root == tmp_path.as_posix()
    assert row.task_cwd == external_task_cwd.as_posix()
