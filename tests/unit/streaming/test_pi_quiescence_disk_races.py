# qa-validated: pi-rpc-quiescence
"""Pi quiescence disk-race regression tests."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import ConnectionConfig, HarnessConnection
from meridian.lib.harness.semantics import TerminalEventOutcome
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.streaming.pi_drain import PiDrainCoordinator, PiSubspawnTracker
from meridian.lib.streaming.spawn_manager import SpawnManager
from tests.unit.streaming.pi_quiescence_test_helpers import (
    FakePiConnection as _FakePiConnection,
)
from tests.unit.streaming.pi_quiescence_test_helpers import (
    NoopControlServer as _NoopControlServer,
)
from tests.unit.streaming.pi_quiescence_test_helpers import (
    pi_event as _pi_event,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_micro_drain_timeout_rechecks_disk_before_accepting(tmp_path: Path) -> None:
    phases: list[str] = []

    def emit_phase(*, phase: str, session_role: str | None, **payload: object) -> None:
        _ = session_role, payload
        phases.append(phase)

    spawn_id = SpawnId("p-micro-drain-recheck")
    connection = _FakePiConnection([])
    await connection.start(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    coordinator = PiDrainCoordinator.for_connection(
        runtime_root=tmp_path,
        spawn_id=spawn_id,
        receiver=connection,
        session_role="spawned",
        notification_timeout_seconds=None,
        child_wave_timeout_seconds=None,
        emit_phase=emit_phase,
    )
    await coordinator.start()
    coordinator.quiescence_enabled = True
    terminal = TerminalEventOutcome(status="succeeded", exit_code=0, error=None)
    coordinator.start_micro_drain(terminal)

    _write_json(
        tmp_path / "spawns" / "p-disk-child" / "state.json",
        {"id": "p-disk-child", "parent_id": str(spawn_id), "status": "running"},
    )

    async def _noop_terminate(
        tracker: PiSubspawnTracker,
        reason: str,
    ) -> None:
        _ = tracker, reason

    try:
        result = await coordinator.handle_timeout(_noop_terminate)
        assert result is None
        assert coordinator.quiescence_candidate is None
        assert "quiescence_micro_drain_cancelled" in phases
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_micro_drain_recheck_preserves_idle_epoch_for_notifications(
    tmp_path: Path,
) -> None:
    phases: list[str] = []

    def emit_phase(*, phase: str, session_role: str | None, **payload: object) -> None:
        _ = session_role, payload
        phases.append(phase)

    spawn_id = SpawnId("p-micro-drain-notification")
    connection = _FakePiConnection([])
    await connection.start(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    coordinator = PiDrainCoordinator.for_connection(
        runtime_root=tmp_path,
        spawn_id=spawn_id,
        receiver=connection,
        session_role="spawned",
        notification_timeout_seconds=None,
        child_wave_timeout_seconds=None,
        emit_phase=emit_phase,
    )
    await coordinator.start()
    coordinator.quiescence_enabled = True
    await coordinator.quiescence_tracker.mark_idle()
    terminal = TerminalEventOutcome(status="succeeded", exit_code=0, error=None)
    coordinator.start_micro_drain(terminal)
    _write_json(
        tmp_path / "pi-bash" / str(spawn_id) / "last-notification.json",
        {"ts_epoch_secs": time.time()},
    )

    async def _noop_terminate(
        tracker: PiSubspawnTracker,
        reason: str,
    ) -> None:
        _ = tracker, reason

    try:
        result = await coordinator.handle_timeout(_noop_terminate)
        assert result is None
        assert coordinator.quiescence_candidate is None
        assert "quiescence_micro_drain_cancelled" in phases
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_micro_drain_cancel_arms_child_wave_for_rescan_discovered_child(
    tmp_path: Path,
) -> None:
    phases: list[dict[str, object]] = []

    def emit_phase(*, phase: str, session_role: str | None, **payload: object) -> None:
        phases.append({"phase": phase, **payload})
        _ = session_role

    spawn_id = SpawnId("p-micro-drain-child-wave")
    connection = _FakePiConnection([])
    await connection.start(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )
    coordinator = PiDrainCoordinator.for_connection(
        runtime_root=tmp_path,
        spawn_id=spawn_id,
        receiver=connection,
        session_role="spawned",
        notification_timeout_seconds=None,
        child_wave_timeout_seconds=1.0,
        emit_phase=emit_phase,
    )
    await coordinator.start()
    coordinator.quiescence_enabled = True
    await coordinator.quiescence_tracker.mark_idle()
    terminal = TerminalEventOutcome(status="succeeded", exit_code=0, error=None)
    coordinator.start_micro_drain(terminal)
    (tmp_path / "spawns" / "p123").mkdir(parents=True)

    async def _noop_terminate(
        tracker: PiSubspawnTracker,
        reason: str,
    ) -> None:
        _ = tracker, reason

    try:
        result = await coordinator.handle_timeout(_noop_terminate)
        assert result is None
        assert coordinator.quiescence_candidate is None
        assert coordinator.child_wave_deadline_monotonic is not None
        assert coordinator.pending_child_count() == 1
        assert any(
            phase.get("phase") == "waiting_for_tracked_children"
            and phase.get("active_tracked_count") == 1
            for phase in phases
        )
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_spawn_manager_pi_drain_loop_reevaluates_on_disk_wakeup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.streaming.pi_drain.PI_MICRO_DRAIN_TIMEOUT_SECONDS",
        0.2,
    )
    disk_wakeup = asyncio.Event()

    async def _fake_wait_for_disk_change(self: object) -> None:
        await disk_wakeup.wait()
        disk_wakeup.clear()
        watcher = getattr(self, "_disk_watcher", None)
        assert watcher is not None, (
            "PiQuiescenceTracker._disk_watcher must be set after start(); "
            "quiescence was enabled but disk watcher was never initialized"
        )
        await watcher.force_rescan()

    monkeypatch.setattr(
        "meridian.lib.streaming.pi_quiescence.PiQuiescenceTracker.wait_for_disk_change",
        _fake_wait_for_disk_change,
    )

    spawn_id = SpawnId("p-disk-wakeup")
    child_state = tmp_path / "spawns" / "p-disk-wait" / "state.json"
    _write_json(
        child_state,
        {"id": "p-disk-wait", "parent_id": str(spawn_id), "status": "running"},
    )

    class _OpenAfterTerminalConnection(_FakePiConnection):
        async def events(self):  # type: ignore[no-untyped-def]
            yield _pi_event("session", {"id": "ses-pi"})
            yield _pi_event(
                "agent_end",
                {"messages": [{"role": "assistant", "stopReason": "stop"}]},
            )
            await asyncio.sleep(60)

    fake_connection = _OpenAfterTerminalConnection([])

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await fake_connection.start(config, spec)
        return fake_connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        completion = asyncio.create_task(manager.wait_for_completion(spawn_id))
        await asyncio.sleep(0.05)
        assert not completion.done()
        _write_json(
            child_state,
            {"id": "p-disk-wait", "parent_id": str(spawn_id), "status": "succeeded"},
        )
        disk_wakeup.set()
        outcome = await asyncio.wait_for(completion, timeout=1.0)
        assert outcome is not None
        assert outcome.status == "succeeded"
        history_path = tmp_path / "spawns" / str(spawn_id) / "history.jsonl"
        history = [
            json.loads(line)
            for line in history_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        phases = [
            event.get("payload", {}).get("phase")
            for event in history
            if event.get("event_type") == "meridian.pi.lifecycle.phase"
        ]
        assert "quiescence_micro_drain_started" in phases
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_disk_change_reevaluation_starts_child_wave_while_parent_idle(
    tmp_path: Path,
) -> None:
    phases: list[dict[str, object]] = []

    def emit_phase(*, phase: str, session_role: str | None, **payload: object) -> None:
        phases.append({"phase": phase, **payload})
        _ = session_role

    spawn_id = SpawnId("p-disk-child-wave")
    connection = _FakePiConnection([])
    await connection.start(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )
    coordinator = PiDrainCoordinator.for_connection(
        runtime_root=tmp_path,
        spawn_id=spawn_id,
        receiver=connection,
        session_role="spawned",
        notification_timeout_seconds=None,
        child_wave_timeout_seconds=1.0,
        emit_phase=emit_phase,
    )
    await coordinator.start()
    coordinator.quiescence_enabled = True
    await coordinator.quiescence_tracker.mark_idle()
    _write_json(
        tmp_path / "spawns" / "p-late-child" / "state.json",
        {"id": "p-late-child", "parent_id": str(spawn_id), "status": "running"},
    )

    try:
        await coordinator.reevaluate_after_disk_change()
        assert coordinator.child_wave_deadline_monotonic is not None
        assert coordinator.pending_child_count() == 1
        assert any(
            phase.get("phase") == "waiting_for_tracked_children"
            and phase.get("active_tracked_count") == 1
            for phase in phases
        )
    finally:
        await coordinator.stop()
