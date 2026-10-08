"""Regression coverage for the native Pi settlement/completion boundaries."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.semantics import normalize_event
from meridian.lib.streaming.drain_policy import PersistentDrainPolicy
from meridian.lib.streaming.spawn_manager import SpawnManager
from tests.support.pi import (
    FakePiConnection,
    NoopControlServer,
    PiDrainScenario,
    _config,
    _spec,
    pi_event,
    start_row,
    write_pi_bash_record,
)


class _OpenPiConnection(FakePiConnection):
    @property
    def subprocess_pid(self) -> int | None:
        return None

    async def events(self):  # type: ignore[no-untyped-def]
        yield pi_event("agent_start")
        yield pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "stop"}]},
        )
        yield pi_event("agent_settled", {"aborted": False})
        await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [True, False])
async def test_spawn_manager_publishes_refined_success_boundary(
    tmp_path: Path, persistent: bool
) -> None:
    spawn_id = SpawnId("p-thermo-boundary")
    start_row(tmp_path, str(spawn_id), HarnessId.PI, None)
    write_pi_bash_record(tmp_path, spawn_id, running=True)
    connection = _OpenPiConnection([])
    observed: list[RawHarnessEvent] = []

    async def start_connection(config, spec):
        await connection.start(config, spec)
        return connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        start_connection=start_connection,
        control_server_factory=lambda *_: NoopControlServer(),
    )
    try:
        await manager.start_spawn(
            _config(tmp_path, spawn_id),
            _spec(),
            drain_policy=PersistentDrainPolicy() if persistent else None,
            event_hook=observed.append,
        )
        for _ in range(100):
            boundaries = [
                event
                for event in observed
                if event.event_type == "meridian/turn_completed"
            ]
            if boundaries:
                break
            await asyncio.sleep(0.01)
        assert boundaries
        assert boundaries[0].payload["status"] == "succeeded"
        assert boundaries[0].payload["exit_code"] == 0
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_compaction_cancels_pending_success_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = await PiDrainScenario.start(
        tmp_path,
        monkeypatch,
        patch_clock=True,
        start_micro_drain=True,
    )
    coordinator = started.coordinator
    try:
        assert coordinator._coordinator.state.phase == "stabilizing"
        started.clock.advance(0.1)
        first = await coordinator.handle_timeout()
        assert first.recorded_outcome is None
        assert coordinator._coordinator._success_validation is not None

        compaction = pi_event("compaction_start", {"reason": "manual"})
        refined = await coordinator.observe_event(normalize_event(compaction))
        assert refined is not None
        assert refined.semantics.activity == "turn_active"
        coordinator.note_event_delivered(compaction)
        result = await coordinator.after_event()
        assert result.recorded_outcome is None
        assert coordinator._coordinator.state.phase != "stabilizing"
    finally:
        await started.stop()


def test_pi_parser_keeps_one_latest_assistant_candidate() -> None:
    event = pi_event(
        "agent_end",
        {
            "messages": [
                {"role": "assistant", "stopReason": "error", "errorMessage": "REAL_FAILURE"},
                {"role": " assistant ", "stopReason": "stop"},
            ]
        },
    )
    outcome = normalize_event(event).semantics.terminal
    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.error == "REAL_FAILURE"

