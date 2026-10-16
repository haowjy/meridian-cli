"""Drain-plan selection and synthetic phase-event characterization."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import ConnectionConfig, RawHarnessEvent
from meridian.lib.harness.registry import get_harness_bundle
from meridian.lib.streaming.drain_coordinator import DrainPlan
from meridian.lib.streaming.pi_drain import PiDrainCoordinator
from meridian.lib.streaming.spawn_manager import SpawnManager


def _select_plan(
    manager: SpawnManager,
    *,
    harness_id: HarnessId,
    resident_backend: object | None = None,
) -> DrainPlan:
    receiver = cast(
        "Any",
        SimpleNamespace(
            harness_id=harness_id,
            resident_backend=resident_backend,
        ),
    )
    return manager._select_drain_plan(
        spawn_id=SpawnId(f"p-{harness_id}"),
        receiver=receiver,
        config=ConnectionConfig(
            spawn_id=SpawnId(f"p-{harness_id}"),
            harness_id=harness_id,
            prompt="hello",
            control_root=manager.runtime_root,
            child_env={},
            pi_session_role="spawned" if harness_id is HarnessId.PI else None,
            pi_child_wave_timeout_seconds=12.0,
            resident_deadline_seconds=13.0,
            resident_poll_seconds=14.0,
        ),
    )


def test_pi_phase_events_update_sidecar_without_history(tmp_path: Path) -> None:
    from tests.support.pi import start_row

    start_row(tmp_path, "p-pi", HarnessId.PI, None)
    manager = SpawnManager(runtime_root=tmp_path, project_root=tmp_path)
    plan = _select_plan(manager, harness_id=HarnessId.PI)
    assert isinstance(plan.coordinator, PiDrainCoordinator)

    for sink in get_harness_bundle(HarnessId.PI).event_sinks(tmp_path, SpawnId("p-pi")):
        manager.register_event_hook(SpawnId("p-pi"), sink)

    for phase, status in (
        ("initial_prompt_sent", None),
        ("cleanup_escalated", "escalated"),
        ("cleanup_completed", "completed"),
    ):
        payload: dict[str, object] = {"phase": phase}
        if status:
            payload["cleanup_status"] = status
        manager.emit_event(
            SpawnId("p-pi"),
            RawHarnessEvent(
                event_type="meridian.pi.lifecycle.phase",
                harness_id="pi",
                payload=payload,
                raw_text=None,
            ),
        )

    sidecar = tmp_path / "spawns" / "p-pi" / "pi-lifecycle.json"
    recorded = json.loads(sidecar.read_text(encoding="utf-8"))
    assert recorded["phase"] == "cleanup_completed"
    assert recorded["cleanup_status"] == "escalated"
    assert not (tmp_path / "spawns" / "p-pi" / "history.jsonl").exists()


def test_late_pi_phase_cannot_recreate_deleted_spawn(tmp_path):
    from meridian.lib.state import pi_lifecycle
    from meridian.lib.state.spawn_aggregate import delete_published_spawn
    from tests.support.pi import start_row

    spawn = SpawnId("p1")
    start_row(tmp_path, spawn, HarnessId.PI, None)
    sinks = get_harness_bundle(HarnessId.PI).event_sinks(tmp_path, spawn)
    assert delete_published_spawn(tmp_path, spawn, can_delete=lambda row: row is not None)
    for sink in sinks:
        sink(
            RawHarnessEvent(
                harness_id="pi",
                event_type="meridian.pi.lifecycle.phase",
                payload={"phase": "cleanup_completed"},
            )
        )
    assert not (tmp_path / "spawns" / spawn).exists()
    assert pi_lifecycle.read(tmp_path, spawn).phase is None
