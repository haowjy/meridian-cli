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
from meridian.lib.state import spawn_store
from meridian.lib.state.spawn_signals import spawn_signal_path
from meridian.lib.streaming import pi_drain as pi_drain_module
from meridian.lib.streaming.completion_nudge import PI_COMPLETION_NUDGE_MESSAGE
from meridian.lib.streaming.drain_policy import DrainAction
from meridian.lib.streaming.pi_drain import PiDrainCoordinator
from meridian.lib.streaming.pi_quiescence import PiQuiescenceTracker
from meridian.lib.streaming.spawn_manager import SpawnManager
from tests.support.async_determinism import AsyncDeterminism, assert_still_pending, wait_until
from tests.support.pi import FakePiConnection as _FakePiConnection
from tests.support.pi import NoopControlServer as _NoopControlServer
from tests.support.pi import PiDrainScenario
from tests.support.pi import pi_event as _pi_event
from tests.support.resident_drain import start_row


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
    nudge_idle_seconds: float = 0.0,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> PiDrainCoordinator:
    assert monkeypatch is not None
    scenario = await PiDrainScenario.start(
        tmp_path,
        monkeypatch,
        spawn_id=spawn_id,
        nudge_idle_seconds=nudge_idle_seconds,
        nudge_interval_seconds=0.05,
        sent_messages=sent_messages,
        patch_clock=False,
    )
    return scenario.coordinator


async def _put_pi_parent_idle_after_success(coordinator: PiDrainCoordinator) -> None:
    outcome = TerminalEventOutcome(status="succeeded", exit_code=0, error=None)
    agent_end = _pi_event("agent_end")
    await coordinator.observe_event(agent_end, "idle")
    await coordinator.handle_terminal_event(
        agent_end, outcome, DrainAction(terminate=True, emit_turn_boundary=False)
    )


async def _started_micro_drain_coordinator(
    tmp_path: Path,
    *,
    spawn_id: SpawnId,
    child_wave_timeout_seconds: float | None = None,
    mark_idle: bool = False,
    start_micro_drain: bool = True,
    cancel_descendants: Any = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> PiDrainScenario:
    assert monkeypatch is not None
    return await PiDrainScenario.start(
        tmp_path,
        monkeypatch,
        spawn_id=spawn_id,
        child_wave_timeout_seconds=child_wave_timeout_seconds,
        mark_idle=mark_idle,
        start_micro_drain=start_micro_drain,
        cancel_descendants=cancel_descendants,
        patch_clock=False,
    )


async def _arm_child_wave(started: PiDrainScenario) -> None:
    started.row("p-stuck-child", parent_id=str(started.spawn_id))
    await started.coordinator.observe_event(_pi_event("agent_end"), "idle")


@pytest.mark.asyncio
async def test_child_wave_timeout_fails_when_cleanup_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = AsyncDeterminism(start=100.0)
    clock.install(monkeypatch, monotonic_modules=(pi_drain_module,))
    spawn_id = SpawnId("p-child-wave-single-timeout")
    cleanup_reasons: list[str] = []

    async def _record_cleanup(reason: str) -> None:
        cleanup_reasons.append(reason)

    started = await _started_micro_drain_coordinator(
        tmp_path,
        monkeypatch=monkeypatch,
        spawn_id=spawn_id,
        child_wave_timeout_seconds=0.01,
        start_micro_drain=False,
        cancel_descendants=_record_cleanup,
    )

    try:
        await _arm_child_wave(started)
        assert started.coordinator.next_timeout() == pytest.approx(0.01)
        clock.advance(0.009)
        before_deadline = await started.coordinator.handle_timeout()
        assert before_deadline.recorded_outcome is None
        assert cleanup_reasons == []
        clock.advance(0.001)

        decision = await started.coordinator.handle_timeout()

        assert cleanup_reasons == []
        exit_decision = await started.coordinator.handle_stream_exit(decision.recorded_outcome)
        request = exit_decision.post_publication_cleanup
        assert request is not None
        await started.coordinator.execute_post_publication_cleanup(request)
        assert cleanup_reasons == ["pi_child_wave_timeout"]
        assert decision.recorded_outcome is not None
        assert decision.recorded_outcome.status == "failed"
        assert decision.recorded_outcome.error == "pi_child_wave_timeout"
    finally:
        await started.coordinator.stop()


@pytest.mark.asyncio
async def test_child_wave_timeout_stays_terminal_when_cleanup_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_id = SpawnId("p122")
    cleanup_reasons: list[str] = []

    async def _raise_cleanup(reason: str) -> None:
        cleanup_reasons.append(reason)
        nested_decision = await started.coordinator.handle_timeout()
        assert nested_decision.recorded_outcome is None
        raise RuntimeError("cleanup failed")

    started = await _started_micro_drain_coordinator(
        tmp_path,
        monkeypatch=monkeypatch,
        spawn_id=spawn_id,
        child_wave_timeout_seconds=0.01,
        start_micro_drain=False,
        cancel_descendants=_raise_cleanup,
    )

    try:
        determinism = AsyncDeterminism(start=100.0)
        determinism.install(monkeypatch, monotonic_modules=(pi_drain_module,))
        await _arm_child_wave(started)
        assert any(
            phase.get("phase") == "waiting_for_tracked_children"
            and phase.get("active_tracked_count") == 1
            for phase in started.phases
        )
        phases_before_timeout = len(started.phases)
        started.phase_errors_enabled.set()
        determinism.advance(0.01)

        decision = await started.coordinator.handle_timeout()
        assert cleanup_reasons == []
        exit_decision = await started.coordinator.handle_stream_exit(decision.recorded_outcome)
        request = exit_decision.post_publication_cleanup
        assert request is not None
        await started.coordinator.execute_post_publication_cleanup(request)
        repeated_decision = await started.coordinator.handle_timeout()

        assert cleanup_reasons == ["pi_child_wave_timeout"]
        assert decision.recorded_outcome is not None
        assert decision.recorded_outcome.status == "failed"
        assert decision.recorded_outcome.error == "pi_child_wave_timeout"
        assert repeated_decision.recorded_outcome is None
        assert any(
            phase.get("phase") == "pi_child_wave_timeout"
            and phase.get("cleanup_error") == "cleanup failed"
            for phase in started.phases
        )
        assert [phase["phase"] for phase in started.phases[phases_before_timeout:]] == [
            "pi_child_wave_timeout"
        ]
    finally:
        await started.coordinator.stop()


@pytest.mark.asyncio
async def test_child_wave_timeout_cleanup_cancellation_keeps_descendant_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_reasons: list[str] = []

    async def _cancel_cleanup(reason: str) -> None:
        cleanup_reasons.append(reason)
        raise asyncio.CancelledError

    started = await _started_micro_drain_coordinator(
        tmp_path,
        monkeypatch=monkeypatch,
        spawn_id=SpawnId("p-child-wave-cleanup-cancelled"),
        child_wave_timeout_seconds=0.01,
        start_micro_drain=False,
        cancel_descendants=_cancel_cleanup,
    )

    try:
        determinism = AsyncDeterminism(start=100.0)
        determinism.install(monkeypatch, monotonic_modules=(pi_drain_module,))
        await _arm_child_wave(started)
        determinism.advance(0.01)

        decision = await started.coordinator.handle_timeout()
        exit_decision = await started.coordinator.handle_stream_exit(decision.recorded_outcome)
        request = exit_decision.post_publication_cleanup
        assert request is not None
        with pytest.raises(asyncio.CancelledError):
            await started.coordinator.execute_post_publication_cleanup(request)
        repeated_decision = await started.coordinator.handle_timeout()
        exit_decision = await started.coordinator.handle_stream_exit(None)

        assert cleanup_reasons == ["pi_child_wave_timeout"]
        assert repeated_decision.recorded_outcome is None
        assert exit_decision.recorded_outcome is not None
        assert exit_decision.recorded_outcome.error == "pi_process_exited_with_tracked_children"
    finally:
        await started.coordinator.stop()


@pytest.mark.asyncio
async def test_pi_non_spawn_background_only_nudges_after_idle_delay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn_id = SpawnId("p-non-spawn-nudge")
    sent_messages: list[str] = []
    _write_running_bash_record(tmp_path, spawn_id, running=True)
    coordinator = await _started_pi_coordinator(
        tmp_path,
        monkeypatch=monkeypatch,
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
async def test_pi_reconciled_terminal_child_allows_done_nudge_for_private_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    spawn_store.mark_finalizing(tmp_path, child_id)
    (tmp_path / "spawns" / str(child_id) / "report.md").write_text(
        "# Report\n\nChild completed.\n",
        encoding="utf-8",
    )
    coordinator = await _started_pi_coordinator(
        tmp_path,
        monkeypatch=monkeypatch,
        spawn_id=spawn_id,
        sent_messages=sent_messages,
    )

    try:
        await _put_pi_parent_idle_after_success(coordinator)

        outstanding = coordinator.classify_outstanding_work()
        await coordinator.handle_timeout()

        assert outstanding.spawn_children is False
        assert outstanding.non_spawn_processes is True
        assert sent_messages == [PI_COMPLETION_NUDGE_MESSAGE]
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_pi_spawn_child_outstanding_waits_without_done_nudge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        monkeypatch=monkeypatch,
        spawn_id=spawn_id,
        sent_messages=sent_messages,
    )

    try:
        await _put_pi_parent_idle_after_success(coordinator)

        outstanding = coordinator.classify_outstanding_work()
        await coordinator.handle_timeout()

        assert outstanding.spawn_children is True
        assert sent_messages == []
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
        monkeypatch=monkeypatch,
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn_id = SpawnId("p-nudge-stops")
    sent_messages: list[str] = []
    _write_running_bash_record(tmp_path, spawn_id, running=True)
    coordinator = await _started_pi_coordinator(
        tmp_path,
        monkeypatch=monkeypatch,
        spawn_id=spawn_id,
        sent_messages=sent_messages,
    )

    try:
        await _put_pi_parent_idle_after_success(coordinator)
        _write_running_bash_record(tmp_path, spawn_id, running=False)
        await coordinator.reevaluate_after_disk_change()

        await coordinator.handle_timeout()

        assert sent_messages == []
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_pi_done_nudge_stops_when_done_signal_arrives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn_id = SpawnId("p-nudge-done")
    sent_messages: list[str] = []
    _write_running_bash_record(tmp_path, spawn_id, running=True)
    coordinator = await _started_pi_coordinator(
        tmp_path,
        monkeypatch=monkeypatch,
        spawn_id=spawn_id,
        sent_messages=sent_messages,
    )

    try:
        await _put_pi_parent_idle_after_success(coordinator)
        await coordinator.handle_timeout()
        assert sent_messages == [PI_COMPLETION_NUDGE_MESSAGE]

        done_signal = spawn_signal_path(tmp_path, spawn_id, "done")
        done_signal.parent.mkdir(parents=True, exist_ok=True)
        done_signal.write_text("test\n", encoding="utf-8")
        decision = await coordinator.handle_timeout()

        assert decision.recorded_outcome is not None
        assert decision.recorded_outcome.status == "succeeded"
        assert sent_messages == [PI_COMPLETION_NUDGE_MESSAGE]
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_micro_drain_timeout_rechecks_disk_before_accepting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn_id = SpawnId("p-micro-drain-recheck")
    started = await _started_micro_drain_coordinator(
        tmp_path,
        monkeypatch=monkeypatch,
        spawn_id=spawn_id,
    )

    start_row(tmp_path, "p-disk-child", HarnessId.CODEX, str(spawn_id))

    try:
        result = await started.coordinator.handle_timeout()
        assert result.recorded_outcome is None
        assert any(
            phase.get("phase") == "quiescence_micro_drain_cancelled" for phase in started.phases
        )
    finally:
        await started.coordinator.stop()


@pytest.mark.asyncio
async def test_micro_drain_recheck_preserves_idle_epoch_for_notifications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn_id = SpawnId("p-micro-drain-notification")
    started = await _started_micro_drain_coordinator(
        tmp_path,
        monkeypatch=monkeypatch,
        spawn_id=spawn_id,
        mark_idle=True,
    )
    _write_json(
        tmp_path / "pi-bash" / str(spawn_id) / "last-notification.json",
        {"ts_epoch_secs": time.time()},
    )

    try:
        result = await started.coordinator.handle_timeout()
        assert result.recorded_outcome is None
        assert any(
            phase.get("phase") == "quiescence_micro_drain_cancelled" for phase in started.phases
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

    async def _fake_wait_for_disk_change(self: PiQuiescenceTracker) -> None:
        await disk_wakeup.wait()
        disk_wakeup.clear()
        await self.refresh_disk_state()

    monkeypatch.setattr(
        "meridian.lib.streaming.pi_quiescence.PiQuiescenceTracker.wait_for_disk_change",
        _fake_wait_for_disk_change,
    )

    spawn_id = SpawnId("p-disk-wakeup")
    start_row(tmp_path, str(spawn_id), HarnessId.PI, None)
    start_row(tmp_path, "p123", HarnessId.CODEX, str(spawn_id))

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
            child_env={},
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
        spawn_store.finalize_spawn(
            tmp_path,
            SpawnId("p123"),
            "succeeded",
            0,
            origin="runner",
        )
        disk_wakeup.set()
        outcome = await completion
        assert outcome is not None
        assert outcome.status == "succeeded"
        history_path = tmp_path / "spawns" / str(spawn_id) / "history.jsonl"

        def _history_has_micro_drain_phase() -> bool:
            return history_path.exists() and any(
                json.loads(line).get("payload", {}).get("phase") == "quiescence_micro_drain_started"
                for line in history_path.read_text(encoding="utf-8").splitlines()
                if line
            )

        await wait_until(
            _history_has_micro_drain_phase,
            timeout=5.0,
            description="quiescence_micro_drain_started lifecycle phase",
        )
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn_id = SpawnId("p-disk-child-wave")
    started = await _started_micro_drain_coordinator(
        tmp_path,
        monkeypatch=monkeypatch,
        spawn_id=spawn_id,
        child_wave_timeout_seconds=1.0,
        mark_idle=True,
        start_micro_drain=False,
    )
    start_row(tmp_path, "p-late-child", HarnessId.CODEX, str(spawn_id))

    try:
        await started.coordinator.reevaluate_after_disk_change()
        assert any(
            phase.get("phase") == "waiting_for_tracked_children"
            and phase.get("active_tracked_count") == 1
            for phase in started.phases
        )
    finally:
        await started.coordinator.stop()
