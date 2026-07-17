"""Drain-plan selection and synthetic phase-event characterization."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import ConnectionConfig, HarnessEvent
from meridian.lib.streaming.drain_coordinator import DrainPlan
from meridian.lib.streaming.drain_policy import (
    PiRpcQuiescenceDrainPolicy,
    SingleTurnDrainPolicy,
)
from meridian.lib.streaming.drain_teardown import (
    DefaultDrainSessionTeardown,
    PiDrainSessionTeardown,
)
from meridian.lib.streaming.pi_drain import PiDrainCoordinator
from meridian.lib.streaming.resident_drain import ResidentDrainCoordinator
from meridian.lib.streaming.spawn_manager import SpawnManager
from tests.support.resident_drain import FakeResidentBackendControl

if TYPE_CHECKING:
    import pytest


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
            env_overrides={},
            pi_session_role="spawned" if harness_id is HarnessId.PI else None,
            pi_notification_timeout_seconds=11.0,
            pi_child_wave_timeout_seconds=12.0,
            resident_deadline_seconds=13.0,
            resident_poll_seconds=14.0,
        ),
    )


def test_spawn_manager_selects_complete_drain_plan_by_connection_capability(
    tmp_path: Path,
) -> None:
    manager = SpawnManager(runtime_root=tmp_path, project_root=tmp_path)

    plain = _select_plan(manager, harness_id=HarnessId.CODEX)
    assert plain == DrainPlan()
    assert isinstance(plain.teardown, DefaultDrainSessionTeardown)

    resident = _select_plan(
        manager,
        harness_id=HarnessId.PI,
        resident_backend=FakeResidentBackendControl(),
    )
    assert isinstance(resident.coordinator, ResidentDrainCoordinator)
    assert isinstance(resident.policy, SingleTurnDrainPolicy)
    assert resident.raw_terminal_frames_authoritative is False
    assert resident.on_policy_selected is None
    assert resident.aux_wake is None
    assert resident.handle_aux_wake is None
    assert resident.finalizer is None
    assert isinstance(resident.teardown, DefaultDrainSessionTeardown)

    pi = _select_plan(manager, harness_id=HarnessId.PI)
    assert isinstance(pi.coordinator, PiDrainCoordinator)
    assert isinstance(pi.policy, PiRpcQuiescenceDrainPolicy)
    assert pi.raw_terminal_frames_authoritative is False
    assert pi.on_policy_selected == pi.coordinator.set_policy
    assert pi.aux_wake is pi.coordinator
    assert pi.handle_aux_wake == pi.coordinator.handle_aux_wake
    assert pi.finalizer is pi.coordinator
    assert isinstance(pi.teardown, PiDrainSessionTeardown)


def test_spawn_manager_authored_event_emission_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, HarnessEvent]] = []
    spawn_id = SpawnId("p-pi-phase")
    manager = SpawnManager(runtime_root=tmp_path, project_root=tmp_path)

    class _Writer:
        def write(self, event: HarnessEvent) -> None:
            calls.append(("persist", event))

    class _Observers:
        def dispatch(self, target_spawn_id: SpawnId, event: HarnessEvent) -> None:
            assert target_spawn_id == spawn_id
            calls.append(("dispatch", event))

    class _Tracer:
        def emit(
            self,
            layer: str,
            label: str,
            *,
            data: dict[str, object],
        ) -> None:
            assert layer == "drain"
            assert label == "meridian_authored"
            event = HarnessEvent(
                event_type="meridian.authored",
                harness_id="meridian",
                payload=data,
                raw_text=None,
            )
            calls.append(("trace", event))

    manager._history_writers[spawn_id] = cast("Any", _Writer())
    manager._observers = cast("Any", _Observers())
    def _fan_out(target_spawn_id: SpawnId, event: HarnessEvent | None) -> None:
        assert target_spawn_id == spawn_id
        calls.append(("fan_out", cast("HarnessEvent", event)))

    def _get_tracer(target_spawn_id: SpawnId) -> _Tracer:
        assert target_spawn_id == spawn_id
        return _Tracer()

    monkeypatch.setattr(manager, "_fan_out_event", _fan_out)
    monkeypatch.setattr(manager, "get_tracer", _get_tracer)

    authored_event = HarnessEvent(
        event_type="meridian.authored",
        harness_id="meridian",
        payload={"type": "meridian.authored", "spawn_id": "p-pi-phase"},
        raw_text=None,
    )
    manager.emit_event(spawn_id, authored_event)

    assert [stage for stage, _event in calls] == [
        "persist",
        "dispatch",
        "fan_out",
        "trace",
    ]
    expected = HarnessEvent(
        event_type="meridian.authored",
        harness_id="meridian",
        payload={"type": "meridian.authored", "spawn_id": "p-pi-phase"},
        raw_text=None,
    )
    assert all(event == expected for _stage, event in calls)
