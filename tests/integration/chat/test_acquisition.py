from pathlib import Path

import pytest

from meridian.lib.chat.backend_acquisition import ColdSpawnAcquisition
from meridian.lib.chat.policy import ChatBackendLaunchPlan, default_chat_policy_snapshot
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import ConnectionCapabilities, ConnectionConfig
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


class Connection:
    harness_id = HarnessId.CLAUDE
    spawn_id = SpawnId("s1")
    session_id = None
    subprocess_pid = None
    capabilities = ConnectionCapabilities(
        mid_turn_injection="queue",
        supports_steer=False,
        supports_cancel=True,
        runtime_model_switch=False,
        structured_reasoning=True,
    )

    def health(self):
        return True

    async def send_user_message(self, text):
        pass

    async def send_cancel(self):
        pass

    async def stop(self):
        pass

    async def events(self):
        if False:
            yield None


class SpawnManager:
    def __init__(self):
        self.calls = []
        self.observers = {}
        self.stopped_spawns = []
        self.connection = Connection()
        self.drain_policy = None
        self.started_config = None
        self.fail_start = False
        self.fail_heartbeat = False

    def register_observer(self, spawn_id, observer):
        self.calls.append(("register", spawn_id, observer))
        self.observers[spawn_id] = observer

    def unregister_observer(self, spawn_id, observer):
        self.calls.append(("unregister", spawn_id, observer))
        if self.observers.get(spawn_id) is observer:
            del self.observers[spawn_id]

    async def start_spawn(self, config, spec, *, drain_policy=None, on_event=None):
        self.calls.append(("start", config.spawn_id, spec))
        if self.fail_start:
            raise RuntimeError("boom")
        self.started_config = config
        self.drain_policy = drain_policy
        return self.connection

    async def start_heartbeat(self, spawn_id):
        self.calls.append(("heartbeat", spawn_id, None))
        if self.fail_heartbeat:
            raise RuntimeError("heartbeat boom")

    async def stop_spawn(self, spawn_id):
        self.calls.append(("stop", spawn_id, None))
        self.stopped_spawns.append(spawn_id)


class Normalizer:
    def normalize(self, event):
        return []

    def reset(self):
        pass


class Pipeline:
    async def ingest(self, event):
        pass

    async def on_execution_complete(self, generation=None):
        pass


class PipelineLookup:
    def __init__(self):
        self.pipeline = Pipeline()

    def get_pipeline(self, chat_id):
        return self.pipeline

    def get_policy_snapshot(self, chat_id):
        _ = chat_id
        return default_chat_policy_snapshot()


def _launch_plan_factory(
    *,
    spawn_id: SpawnId,
    harness_id: HarnessId,
    control_root: Path,
    env_overrides: dict[str, str] | None = None,
):
    def build(chat_id: str, prompt: str) -> ChatBackendLaunchPlan:
        _ = chat_id
        config = ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=harness_id,
            prompt=prompt,
            control_root=control_root,
            env_overrides=env_overrides or {},
        )
        spec = ResolvedLaunchSpec(
            prompt=prompt,
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        )
        return ChatBackendLaunchPlan(
            harness_id=harness_id,
            connection_config=config,
            spec=spec,
            configured_payload={"harness": harness_id.value, "model": spec.model or ""},
        )

    return build


@pytest.mark.asyncio
async def test_cold_acquisition_success_returns_usable_execution(
    tmp_path: Path,
):
    manager = SpawnManager()
    spawn_id = SpawnId("s-chat")

    acquisition = ColdSpawnAcquisition(
        spawn_manager=manager,
        normalizer_factory=lambda chat_id, execution_id: Normalizer(),
        pipeline_lookup=PipelineLookup(),
        launch_plan_factory=_launch_plan_factory(
            spawn_id=spawn_id,
            harness_id=HarnessId.CLAUDE,
            control_root=tmp_path,
        ),
    )

    handle = await acquisition.acquire("c1", "hello", execution_generation=7)

    assert manager.started_config.prompt == "hello"
    assert manager.observers.keys() == {spawn_id}
    assert manager.drain_policy is not None
    assert handle.spawn_id == spawn_id
    assert handle.generation == 7
    assert handle.connection is manager.connection
    assert handle.health() is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail_start", "fail_heartbeat", "expected_error"),
    [
        (True, False, "boom"),
        (False, True, "heartbeat boom"),
    ],
    ids=["start-failure", "heartbeat-failure"],
)
async def test_cold_acquisition_failure_cleans_up_execution(
    tmp_path: Path,
    fail_start: bool,
    fail_heartbeat: bool,
    expected_error: str,
):
    manager = SpawnManager()
    manager.fail_start = fail_start
    manager.fail_heartbeat = fail_heartbeat
    spawn_id = SpawnId("s-chat")

    acquisition = ColdSpawnAcquisition(
        spawn_manager=manager,
        normalizer_factory=lambda chat_id, execution_id: Normalizer(),
        pipeline_lookup=PipelineLookup(),
        launch_plan_factory=_launch_plan_factory(
            spawn_id=spawn_id,
            harness_id=HarnessId.CLAUDE,
            control_root=tmp_path,
        ),
    )

    with pytest.raises(RuntimeError, match=expected_error):
        await acquisition.acquire("c1", "hello")

    assert manager.observers == {}
    assert manager.stopped_spawns == [spawn_id]
