# qa-validated: test-suite-redesign
"""Streaming runner seed persistence tests: Claude seed and OpenCode adapter seed port."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest

from meridian.lib.core.domain import Spawn
from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.harness.claude_utils import extract_session_id_from_args
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    HarnessEvent,
)
from meridian.lib.harness.launch_spec import ResolvedLaunchSpec
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.request import LaunchArgvIntent, LaunchRuntime, SpawnRequest
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import LocalStore
from meridian.lib.state.paths import (
    resolve_project_runtime_root,
)
from meridian.lib.streaming import spawn_manager as spawn_manager_module
from tests.support.fakes import FakeClock, FakeHeartbeat

streaming_runner_module = importlib.import_module("meridian.lib.launch.streaming_runner")


class _FakeControlSocketServer:
    def __init__(self, spawn_id: SpawnId, socket_path: Path, manager: object) -> None:
        _ = spawn_id, manager
        self.socket_path = socket_path

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

    async def stop(self) -> None:
        return None


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
        yield HarnessEvent(
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
        yield HarnessEvent(
            event_type="result",
            harness_id="opencode",
            payload={"type": "result", "result": "seeded opencode complete"},
        )


def _build_claude_request() -> SpawnRequest:
    return SpawnRequest(
        model="claude-sonnet-4-6",
        harness=HarnessId.CLAUDE.value,
        prompt="hello",
    )


async def _execute_with_context(
    run: Spawn,
    *,
    request: SpawnRequest,
    project_root: Path,
    runtime_root: Path,
    artifacts: LocalStore,
    registry: HarnessRegistry,
    **kwargs: object,
) -> int:
    launch_context = build_launch_context(
        spawn_id=str(run.spawn_id),
        request=request,
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.SPEC_ONLY,
            runtime_root=runtime_root.as_posix(),
            project_paths_project_root=project_root.as_posix(),
            project_paths_execution_cwd=project_root.resolve().as_posix(),
        ),
        harness_registry=registry,
    )
    return await streaming_runner_module.execute_with_streaming(
        run,
        request=request,
        launch_context=launch_context,
        project_root=project_root,
        runtime_root=runtime_root,
        artifacts=artifacts,
        **kwargs,
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
        lambda _harness_id: _ClaudeSeedPersistenceConnection,
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
        lambda _harness_id: _OpenCodeSeedPortConnection,
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
