"""Delivery is independent of attempt folding and optional runner persistence."""

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.bundle import get_harness_bundle
from meridian.lib.harness.connections.base import ConnectionConfig, RawHarnessEvent
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.streaming.drain_policy import PersistentDrainPolicy
from meridian.lib.streaming.spawn_manager import SpawnManager
from tests.support.pi import NoopControlServer, start_row
from tests.support.resident_drain import FakeResidentConnection


@pytest.mark.asyncio
async def test_delivered_sequence_is_identical_with_and_without_fold(tmp_path, monkeypatch):

    delivered = []
    frames = [
        RawHarnessEvent(event_type=p["type"], harness_id="claude", payload=p)
        for p in [
            {"type": "system", "session_id": "owned"},
            {"type": "assistant", "message": {"content": "draft"}},
            {"type": "result", "result": "final report"},
        ]
    ]
    for with_fold in (False, True):
        runtime = tmp_path / str(with_fold)
        spawn = SpawnId("p1")
        start_row(runtime, str(spawn), HarnessId.CLAUDE, None)
        connection = FakeResidentConnection(HarnessId.CLAUDE)

        async def start(config, spec, connection=connection):
            await connection.start(config, spec)
            return connection

        manager = SpawnManager(
            runtime_root=runtime,
            project_root=runtime,
            start_connection=start,
            control_server_factory=lambda *args: NoopControlServer(),
        )
        fold = get_harness_bundle(HarnessId.CLAUDE).extractor.create_fold()
        try:
            await manager.start_spawn(
                ConnectionConfig(
                    spawn_id=spawn,
                    harness_id=HarnessId.CLAUDE,
                    prompt="test",
                    control_root=runtime,
                    child_env={},
                ),
                ResolvedLaunchSpec(
                    harness="claude",
                    prompt="test",
                    permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
                ),
                drain_policy=PersistentDrainPolicy(),
                event_hook=fold if with_fold else None,
            )
            subscriber = manager.subscribe(spawn)
            for frame in frames:
                connection.emit(frame)
            connection.close_stream()
            await manager.wait_for_completion(spawn)
            received = []
            while not subscriber.empty():
                item = subscriber.get_nowait()
                if item is not None:
                    received.append(item.raw)
            delivered.append(received)
            if with_fold:
                assert fold.facts.final_text == "final report"
            assert not (runtime / "spawns" / spawn / "history.jsonl").exists()
        finally:
            await manager.stop_spawn(spawn)
    assert delivered[0] == delivered[1]
    assert all(frame in delivered[0] for frame in frames)
