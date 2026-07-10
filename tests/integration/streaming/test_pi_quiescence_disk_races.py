# qa-validated: pi-rpc-quiescence
"""Pi quiescence disk-race regression tests."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import ConnectionConfig, HarnessConnection
from meridian.lib.harness.semantics import TerminalEventOutcome
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state import spawn_store
from meridian.lib.state.spawn_signals import write_spawn_signal
from meridian.lib.streaming.completion_nudge import PI_COMPLETION_NUDGE_MESSAGE
from meridian.lib.streaming.drain_policy import DrainAction
from meridian.lib.streaming.pi_drain import PiDrainCoordinator
from meridian.lib.streaming.pi_subspawn_tracker import PiSubspawnTracker
from meridian.lib.streaming.spawn_manager import SpawnManager
from tests.support.async_determinism import AsyncDeterminism, assert_still_pending
from tests.support.pi import (
    FakePiConnection as _FakePiConnection,
)
from tests.support.pi import (
    NoopControlServer as _NoopControlServer,
)
from tests.support.pi import (
    pi_event as _pi_event,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_running_bash_record(runtime_root: Path, spawn_id: SpawnId, *, running: bool) -> None:
    _write_json(
        runtime_root / "pi-bash" / str(spawn_id) / "bash-records.json",
        {
            "records": {
                "b1": {
                    "bash_id": "b1",
                    "is_tracked": True,
                    "is_background": True,
                    "status": "running" if running else "exited",
                }
            }
        },
    )


async def _started_pi_coordinator(
    tmp_path: Path,
    *,
    spawn_id: SpawnId,
    sent_messages: list[str] | None = None,
) -> PiDrainCoordinator:
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

    async def _send_done_nudge(message: str) -> None:
        assert sent_messages is not None
        sent_messages.append(message)

    coordinator = PiDrainCoordinator.for_connection(
        runtime_root=tmp_path,
        spawn_id=spawn_id,
        receiver=connection,
        session_role="spawned",
        notification_timeout_seconds=None,
        child_wave_timeout_seconds=None,
        emit_phase=lambda **_payload: None,
        send_done_nudge=_send_done_nudge if sent_messages is not None else None,
    )
    await coordinator.start()
    coordinator.quiescence_enabled = True
    coordinator.done_nudge_idle_delay_seconds = 0.0
    coordinator.done_nudge_interval_seconds = 0.05
    return coordinator


async def _put_pi_parent_idle_after_success(coordinator: PiDrainCoordinator) -> None:
    await coordinator.quiescence_tracker.mark_idle()
    outcome = TerminalEventOutcome(status="succeeded", exit_code=0, error=None)
    await coordinator.handle_terminal_event(
        _pi_event("agent_end", {}),
        outcome,
        DrainAction(terminate=True, emit_turn_boundary=False),
    )


@dataclass
class _StartedMicroDrain:
    coordinator: PiDrainCoordinator
    phases: list[dict[str, object]]


async def _started_micro_drain_coordinator(
    tmp_path: Path,
    *,
    spawn_id: SpawnId,
    child_wave_timeout_seconds: float | None = None,
    mark_idle: bool = False,
    start_micro_drain: bool = True,
) -> _StartedMicroDrain:
    phases: list[dict[str, object]] = []

    def emit_phase(*, phase: str, session_role: str | None, **payload: object) -> None:
        phases.append({"phase": phase, **payload})
        _ = session_role

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
        child_wave_timeout_seconds=child_wave_timeout_seconds,
        emit_phase=emit_phase,
    )
    await coordinator.start()
    coordinator.quiescence_enabled = True
    if mark_idle:
        await coordinator.quiescence_tracker.mark_idle()
    if start_micro_drain:
        terminal = TerminalEventOutcome(status="succeeded", exit_code=0, error=None)
        coordinator.start_micro_drain(terminal)
    return _StartedMicroDrain(coordinator=coordinator, phases=phases)


async def _noop_terminate(
    tracker: PiSubspawnTracker,
    reason: str,
) -> None:
    _ = tracker, reason


@pytest.mark.asyncio
async def test_pi_non_spawn_background_only_nudges_after_idle_delay(tmp_path: Path) -> None:
    spawn_id = SpawnId("p-non-spawn-nudge")
    sent_messages: list[str] = []
    _write_running_bash_record(tmp_path, spawn_id, running=True)
    coordinator = await _started_pi_coordinator(
        tmp_path,
        spawn_id=spawn_id,
        sent_messages=sent_messages,
    )

    try:
        await _put_pi_parent_idle_after_success(coordinator)

        await coordinator.handle_timeout()

        assert sent_messages == [PI_COMPLETION_NUDGE_MESSAGE]
        assert "meridian spawn done" in sent_messages[0]
        assert "meridian spawn rearm" not in sent_messages[0]
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_pi_non_spawn_tracked_id_nudges_after_idle_delay(tmp_path: Path) -> None:
    spawn_id = SpawnId("p1")
    sent_messages: list[str] = []
    coordinator = await _started_pi_coordinator(
        tmp_path,
        spawn_id=spawn_id,
        sent_messages=sent_messages,
    )
    coordinator.tracker.active_ids.add("pi-internal-1")

    try:
        await _put_pi_parent_idle_after_success(coordinator)

        classified = coordinator.classify_outstanding_work()
        assert classified.spawn_children is False
        assert classified.unknown_spawn_children is False
        assert classified.non_spawn_processes is True
        await coordinator.handle_timeout()

        assert sent_messages == [PI_COMPLETION_NUDGE_MESSAGE]
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_pi_spawn_child_outstanding_waits_without_done_nudge(tmp_path: Path) -> None:
    spawn_id = SpawnId("p1")
    child_id = SpawnId("p2")
    sent_messages: list[str] = []
    _write_running_bash_record(tmp_path, spawn_id, running=True)
    spawn_store.start_spawn(
        tmp_path,
        spawn_id=spawn_id,
        chat_id=str(spawn_id),
        model="test-model",
        agent="test-agent",
        harness=HarnessId.PI.value,
        prompt="parent",
        status="running",
    )
    spawn_store.start_spawn(
        tmp_path,
        spawn_id=child_id,
        chat_id=str(child_id),
        parent_id=str(spawn_id),
        model="test-model",
        agent="test-agent",
        harness=HarnessId.PI.value,
        prompt="child",
        status="running",
    )
    coordinator = await _started_pi_coordinator(
        tmp_path,
        spawn_id=spawn_id,
        sent_messages=sent_messages,
    )

    try:
        await _put_pi_parent_idle_after_success(coordinator)

        await coordinator.handle_timeout()

        assert sent_messages == []
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_pi_spawn_shaped_tracked_child_without_row_waits_closed_then_waits_on_row(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p1")
    child_id = SpawnId("p123")
    sent_messages: list[str] = []
    _write_running_bash_record(tmp_path, spawn_id, running=True)
    spawn_store.start_spawn(
        tmp_path,
        spawn_id=spawn_id,
        chat_id=str(spawn_id),
        model="test-model",
        agent="test-agent",
        harness=HarnessId.PI.value,
        prompt="parent",
        status="running",
    )
    coordinator = await _started_pi_coordinator(
        tmp_path,
        spawn_id=spawn_id,
        sent_messages=sent_messages,
    )
    coordinator.tracker.active_ids.add(str(child_id))

    try:
        await _put_pi_parent_idle_after_success(coordinator)

        rowless = coordinator.classify_outstanding_work()
        assert rowless.spawn_children is False
        assert rowless.unknown_spawn_children is True
        assert rowless.non_spawn_processes is True
        await coordinator.handle_timeout()
        assert sent_messages == []

        spawn_store.start_spawn(
            tmp_path,
            spawn_id=child_id,
            chat_id=str(child_id),
            parent_id=str(spawn_id),
            model="test-model",
            agent="test-agent",
            harness=HarnessId.PI.value,
            prompt="child",
            status="running",
        )

        with_row = coordinator.classify_outstanding_work()
        assert with_row.spawn_children is True
        assert with_row.unknown_spawn_children is False
        assert with_row.non_spawn_processes is True
        await coordinator.handle_timeout()
        assert sent_messages == []
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_pi_candidate_child_dir_before_row_suppresses_done_nudge_until_terminal(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p1")
    child_id = SpawnId("p123")
    sent_messages: list[str] = []
    _write_running_bash_record(tmp_path, spawn_id, running=True)
    spawn_store.start_spawn(
        tmp_path,
        spawn_id=spawn_id,
        chat_id=str(spawn_id),
        model="test-model",
        agent="test-agent",
        harness=HarnessId.PI.value,
        prompt="parent",
        status="running",
    )
    coordinator = await _started_pi_coordinator(
        tmp_path,
        spawn_id=spawn_id,
        sent_messages=sent_messages,
    )

    try:
        await _put_pi_parent_idle_after_success(coordinator)
        (tmp_path / "spawns" / str(child_id)).mkdir(parents=True)
        await coordinator.quiescence_tracker.refresh_disk_state()

        candidate = coordinator.classify_outstanding_work()
        assert candidate.spawn_children is False
        assert candidate.unknown_spawn_children is True
        assert candidate.non_spawn_processes is True
        await coordinator.handle_timeout()
        assert sent_messages == []
        assert coordinator.next_done_nudge_monotonic is None

        spawn_store.start_spawn(
            tmp_path,
            spawn_id=child_id,
            chat_id=str(child_id),
            parent_id=str(spawn_id),
            model="test-model",
            agent="test-agent",
            harness=HarnessId.PI.value,
            prompt="child",
            status="succeeded",
        )
        await coordinator.quiescence_tracker.refresh_disk_state()

        resolved_terminal = coordinator.classify_outstanding_work()
        assert resolved_terminal.spawn_children is False
        assert resolved_terminal.unknown_spawn_children is False
        assert resolved_terminal.non_spawn_processes is True
        await coordinator.handle_timeout()
        assert coordinator.next_done_nudge_monotonic is not None
        await coordinator.handle_timeout()
        assert sent_messages == [PI_COMPLETION_NUDGE_MESSAGE]
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_pi_done_nudge_repeats_on_bounded_cadence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.streaming import pi_drain as pi_drain_module

    determinism = AsyncDeterminism(start=100.0)
    determinism.install(monkeypatch, monotonic_modules=(pi_drain_module,))

    spawn_id = SpawnId("p-nudge-cadence")
    sent_messages: list[str] = []
    _write_running_bash_record(tmp_path, spawn_id, running=True)
    coordinator = await _started_pi_coordinator(
        tmp_path,
        spawn_id=spawn_id,
        sent_messages=sent_messages,
    )

    try:
        await _put_pi_parent_idle_after_success(coordinator)

        await coordinator.handle_timeout()
        await coordinator.handle_timeout()
        assert sent_messages == [PI_COMPLETION_NUDGE_MESSAGE]

        determinism.advance(0.06)
        await coordinator.handle_timeout()

        assert sent_messages == [PI_COMPLETION_NUDGE_MESSAGE, PI_COMPLETION_NUDGE_MESSAGE]
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_pi_done_nudge_stops_when_work_drains_or_done_signal_arrives(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p-nudge-stops")
    sent_messages: list[str] = []
    _write_running_bash_record(tmp_path, spawn_id, running=True)
    coordinator = await _started_pi_coordinator(
        tmp_path,
        spawn_id=spawn_id,
        sent_messages=sent_messages,
    )

    try:
        await _put_pi_parent_idle_after_success(coordinator)
        _write_running_bash_record(tmp_path, spawn_id, running=False)
        await coordinator.quiescence_tracker.refresh_disk_state()

        await coordinator.handle_timeout()

        assert sent_messages == []
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_pi_done_nudge_stops_when_done_signal_arrives(tmp_path: Path) -> None:
    spawn_id = SpawnId("p-nudge-done")
    sent_messages: list[str] = []
    _write_running_bash_record(tmp_path, spawn_id, running=True)
    coordinator = await _started_pi_coordinator(
        tmp_path,
        spawn_id=spawn_id,
        sent_messages=sent_messages,
    )

    try:
        await _put_pi_parent_idle_after_success(coordinator)
        await coordinator.handle_timeout()
        assert sent_messages == [PI_COMPLETION_NUDGE_MESSAGE]

        write_spawn_signal(tmp_path, spawn_id, "done")
        decision = await coordinator.handle_timeout()

        assert decision.recorded_outcome is not None
        assert decision.recorded_outcome.status == "succeeded"
        assert sent_messages == [PI_COMPLETION_NUDGE_MESSAGE]
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_micro_drain_timeout_rechecks_disk_before_accepting(tmp_path: Path) -> None:
    spawn_id = SpawnId("p-micro-drain-recheck")
    started = await _started_micro_drain_coordinator(
        tmp_path,
        spawn_id=spawn_id,
    )

    _write_json(
        tmp_path / "spawns" / "p-disk-child" / "state.json",
        {"id": "p-disk-child", "parent_id": str(spawn_id), "status": "running"},
    )

    try:
        result = await started.coordinator.handle_timeout(_noop_terminate)
        assert result.recorded_outcome is None
        assert started.coordinator.quiescence_candidate is None
        assert any(
            phase.get("phase") == "quiescence_micro_drain_cancelled"
            for phase in started.phases
        )
    finally:
        await started.coordinator.stop()


@pytest.mark.asyncio
async def test_micro_drain_recheck_preserves_idle_epoch_for_notifications(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p-micro-drain-notification")
    started = await _started_micro_drain_coordinator(
        tmp_path,
        spawn_id=spawn_id,
        mark_idle=True,
    )
    _write_json(
        tmp_path / "pi-bash" / str(spawn_id) / "last-notification.json",
        {"ts_epoch_secs": time.time()},
    )

    try:
        result = await started.coordinator.handle_timeout(_noop_terminate)
        assert result.recorded_outcome is None
        assert started.coordinator.quiescence_candidate is None
        assert any(
            phase.get("phase") == "quiescence_micro_drain_cancelled"
            for phase in started.phases
        )
    finally:
        await started.coordinator.stop()


@pytest.mark.asyncio
async def test_micro_drain_cancel_arms_child_wave_for_rescan_discovered_child(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p-micro-drain-child-wave")
    started = await _started_micro_drain_coordinator(
        tmp_path,
        spawn_id=spawn_id,
        child_wave_timeout_seconds=1.0,
        mark_idle=True,
    )
    (tmp_path / "spawns" / "p123").mkdir(parents=True)

    try:
        result = await started.coordinator.handle_timeout(_noop_terminate)
        assert result.recorded_outcome is None
        assert started.coordinator.quiescence_candidate is None
        assert started.coordinator.child_wave_deadline_monotonic is not None
        assert started.coordinator.pending_child_count() == 1
        assert any(
            phase.get("phase") == "waiting_for_tracked_children"
            and phase.get("active_tracked_count") == 1
            for phase in started.phases
        )
    finally:
        await started.coordinator.stop()

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
    child_state = tmp_path / "spawns" / "p123" / "state.json"
    _write_json(
        child_state,
        {"id": "p123", "parent_id": str(spawn_id), "status": "running"},
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
        await assert_still_pending(completion)
        _write_json(
            child_state,
            {"id": "p123", "parent_id": str(spawn_id), "status": "succeeded"},
        )
        disk_wakeup.set()
        outcome = await completion
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
    spawn_id = SpawnId("p-disk-child-wave")
    started = await _started_micro_drain_coordinator(
        tmp_path,
        spawn_id=spawn_id,
        child_wave_timeout_seconds=1.0,
        mark_idle=True,
        start_micro_drain=False,
    )
    _write_json(
        tmp_path / "spawns" / "p-late-child" / "state.json",
        {"id": "p-late-child", "parent_id": str(spawn_id), "status": "running"},
    )

    try:
        await started.coordinator.reevaluate_after_disk_change()
        assert started.coordinator.child_wave_deadline_monotonic is not None
        assert started.coordinator.pending_child_count() == 1
        assert any(
            phase.get("phase") == "waiting_for_tracked_children"
            and phase.get("active_tracked_count") == 1
            for phase in started.phases
        )
    finally:
        await started.coordinator.stop()
