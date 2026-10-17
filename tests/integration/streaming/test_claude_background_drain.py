"""Regression #655: a turn result is not completion while native tasks run."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import (
    ConnectionConfig,
    HarnessConnection,
    RawHarnessEvent,
)
from meridian.lib.harness.semantics import NormalizedHarnessEvent
from meridian.lib.streaming.spawn_drain_loop import SpawnDrainLoop
from meridian.lib.streaming.spawn_manager import SpawnManager
from meridian.lib.streaming.spawn_session import DrainOutcome, SpawnSession


@pytest.mark.asyncio
async def test_claude_waits_for_background_snapshot_and_followup_result(tmp_path: Path) -> None:
    delivered: list[NormalizedHarnessEvent] = []
    outcomes: list[DrainOutcome] = []
    spawn_id = SpawnId("p-claude-bg")

    def frame(kind: str, **payload: object) -> RawHarnessEvent:
        return RawHarnessEvent(event_type=kind, harness_id="claude", payload=payload)

    class Receiver:
        harness_id = HarnessId.CLAUDE
        resident_backend = None
        primary_event_scope = None

        def observe_event_semantics(self, semantics: object) -> None:
            pass

        async def events(self) -> AsyncIterator[RawHarnessEvent]:
            yield frame("system", subtype="background_tasks_changed", tasks=[{"task_id": "bg"}])
            yield frame("result", subtype="success", result="Still running")
            # Neither the first result nor task completion may publish success:
            # Claude's automatic follow-up turn still owes its own result.
            assert not outcomes
            yield frame("system", subtype="task_notification", task_id="bg", status="completed")
            yield frame("system", subtype="background_tasks_changed", tasks=[])
            assert not outcomes
            yield frame("result", subtype="success", result="Marker read: done")

    receiver = cast("HarnessConnection[Any]", Receiver())
    manager = SpawnManager(runtime_root=tmp_path, project_root=tmp_path)
    plan = manager._select_drain_plan(
        spawn_id=spawn_id,
        receiver=receiver,
        config=ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.CLAUDE,
            prompt="probe",
            control_root=tmp_path,
            child_env={},
        ),
    )
    session = cast("SpawnSession", SimpleNamespace(cancel_sent=False, started_monotonic=0.0))

    def publish(
        _id: SpawnId,
        _session: SpawnSession,
        outcome: DrainOutcome,
        _cleanup: object,
    ) -> DrainOutcome:
        outcomes.append(outcome)
        return outcome

    loop = SpawnDrainLoop(
        sessions={spawn_id: session},
        run_event_hooks=lambda *_: None,
        publish_terminal=publish,
        fan_out_event=lambda _, event: delivered.append(event),
        fan_out_turn_boundary=AsyncMock(),
    )
    await loop.run(spawn_id=spawn_id, receiver=receiver, drain_plan=plan, tracer=None)
    assert delivered[-1].raw.payload["result"] == "Marker read: done"
    assert delivered[1].semantics.terminal is None
    assert len(outcomes) == 1
    assert outcomes[0].status == "succeeded"
    assert outcomes[0].authoritative
