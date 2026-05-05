from pathlib import Path

import pytest

from meridian.lib.chat.backend_acquisition import ColdSpawnAcquisition
from meridian.lib.chat.event_log import ChatEventLog
from meridian.lib.chat.event_pipeline import ChatEventPipeline
from meridian.lib.chat.protocol import CHAT_CONFIGURED, TURN_STARTED, ChatEvent
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    HarnessEvent,
)
from meridian.lib.harness.ids import HarnessId
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


class Session:
    def on_turn_completed(self, generation=None):
        _ = generation

    def on_execution_died(self, generation=None):
        _ = generation

    def consume_state_transitions(self):
        return []


class FakePipelineLookup:
    def __init__(self, pipeline):
        self.pipeline = pipeline

    def get_pipeline(self, chat_id):
        _ = chat_id
        return self.pipeline


class FakeConnection:
    capabilities = ConnectionCapabilities(
        mid_turn_injection="queue",
        supports_steer=True,
        supports_cancel=True,
        runtime_model_switch=False,
        structured_reasoning=False,
        supports_runtime_hitl=True,
    )

    def health(self):
        return True


class FakeSpawnManager:
    def __init__(self):
        self.observer = None
        self.spawn_id = None
        self.started = []
        self.heartbeats = []

    def register_observer(self, spawn_id, observer):
        self.spawn_id = spawn_id
        self.observer = observer

    def unregister_observer(self, spawn_id, observer):
        _ = (spawn_id, observer)

    async def start_spawn(self, config, spec, *, drain_policy=None, on_event=None):
        self.started.append((config, spec, drain_policy, on_event))
        return FakeConnection()

    async def start_heartbeat(self, spawn_id):
        self.heartbeats.append(spawn_id)

    async def stop_spawn(self, spawn_id):
        _ = spawn_id


class NoopNormalizer:
    def normalize(self, event):
        _ = event
        return []

    def reset(self):
        pass


def launch_spec(prompt):
    return ResolvedLaunchSpec(
        prompt=prompt,
        model="gpt-test",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )


def connection_config(chat_id, initial_prompt):
    _ = chat_id
    return ConnectionConfig(
        spawn_id=SpawnId("chat-s1"),
        harness_id=HarnessId.CODEX,
        prompt=initial_prompt,
        project_root=Path.cwd(),
        env_overrides={},
    )


@pytest.mark.asyncio
async def test_acquisition_emits_chat_configured_before_observed_turn_started(tmp_path):
    pipeline = ChatEventPipeline("c1", ChatEventLog(tmp_path / "events.jsonl"), Session())
    pipeline.start()
    manager = FakeSpawnManager()
    acquisition = ColdSpawnAcquisition(
        spawn_manager=manager,
        normalizer_factory=lambda chat_id, execution_id: NoopNormalizer(),
        pipeline_lookup=FakePipelineLookup(pipeline),
        connection_config_factory=connection_config,
        launch_spec_factory=launch_spec,
    )

    handle = await acquisition.acquire("c1", "hello", execution_generation=3, chat_state="active")
    await manager.observer.on_event(
        manager.spawn_id,
        HarnessEvent(
            event_type="turn/started",
            payload={},
            harness_id="codex",
        ),
    )
    await pipeline.ingest(
        ChatEvent(
            type=TURN_STARTED,
            seq=0,
            chat_id="c1",
            execution_id=str(handle.spawn_id),
            timestamp="now",
            payload={},
            harness_id="codex",
        )
    )
    await pipeline.drain()
    await pipeline.stop()

    events = list(ChatEventLog(tmp_path / "events.jsonl").read_all())
    assert [event.type for event in events] == [CHAT_CONFIGURED, TURN_STARTED]
    assert events[0].payload == {
        "harness": "codex",
        "model": "gpt-test",
        "state": "active",
        "supports_hitl": True,
        "supports_checkpoints": True,
        "supports_model_swap": False,
        "supports_effort_swap": False,
    }
