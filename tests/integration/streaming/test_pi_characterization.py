"""Fake-clock characterization of Pi drain coordination and current authority."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import ConnectionConfig, HarnessEvent
from meridian.lib.harness.semantics import TerminalEventOutcome
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state import spawn_store
from meridian.lib.state.spawn_signals import write_spawn_signal
from meridian.lib.streaming import descendant_evidence as descendant_evidence_module
from meridian.lib.streaming import pi_completion_profile as pi_completion_profile_module
from meridian.lib.streaming import pi_drain as pi_drain_module
from meridian.lib.streaming.completion_nudge import PI_COMPLETION_NUDGE_MESSAGE
from meridian.lib.streaming.drain_coordinator import DrainExitDecision, DrainLoopDecision
from meridian.lib.streaming.drain_policy import DrainAction, PiRpcQuiescenceDrainPolicy
from meridian.lib.streaming.pi_drain import PiDrainCoordinator
from meridian.lib.streaming.pi_work_ledger import PiPrivateWorkLedger
from tests.support.async_determinism import FakeClock
from tests.support.pi import FakePiConnection, pi_event

_SPAWN_ID = SpawnId("p1")
_SUCCESS = TerminalEventOutcome(status="succeeded", exit_code=0)
_TERMINATE = DrainAction(terminate=True, emit_turn_boundary=False)
_AGENT_END = pi_event("agent_end", {})


@dataclass
class _StartedCoordinator:
    coordinator: PiDrainCoordinator
    clock: FakeClock
    phases: list[dict[str, object]]
    nudges: list[str]
    cleanups: list[str]


async def _start_coordinator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    spawn_id: SpawnId = _SPAWN_ID,
    notification_timeout_seconds: float | None = None,
    child_wave_timeout_seconds: float | None = None,
    nudge_idle_seconds: float = 5.0,
    nudge_raises: bool = False,
    cleanup_configured: bool = True,
) -> _StartedCoordinator:
    clock = FakeClock(start=100.0)
    monkeypatch.setattr(pi_drain_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(
        pi_completion_profile_module,
        "PI_DONE_NUDGE_IDLE_DELAY_SECONDS",
        nudge_idle_seconds,
    )
    monkeypatch.setattr(
        pi_completion_profile_module,
        "COMPLETION_NUDGE_INTERVAL_SECONDS",
        5.0,
    )
    phases: list[dict[str, object]] = []
    nudges: list[str] = []
    cleanups: list[str] = []

    connection = FakePiConnection([])
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

    def _emit_phase(*, phase: str, session_role: str | None, **payload: object) -> None:
        phases.append({"phase": phase, **payload})
        assert session_role == "spawned"

    async def _nudge(message: str) -> None:
        nudges.append(message)
        if nudge_raises:
            raise RuntimeError("advisory nudge failed")

    async def _cleanup(_ledger: PiPrivateWorkLedger, reason: str) -> None:
        cleanups.append(reason)

    coordinator = PiDrainCoordinator.for_connection(
        runtime_root=tmp_path,
        spawn_id=spawn_id,
        receiver=connection,
        session_role="spawned",
        notification_timeout_seconds=notification_timeout_seconds,
        child_wave_timeout_seconds=child_wave_timeout_seconds,
        emit_phase=_emit_phase,
        terminate_children=_cleanup if cleanup_configured else None,
        send_done_nudge=_nudge,
    )
    await coordinator.start()
    coordinator.set_policy(
        PiRpcQuiescenceDrainPolicy(quiescence_check=coordinator.is_quiescent)
    )
    return _StartedCoordinator(coordinator, clock, phases, nudges, cleanups)


def _tracked_child_start(child_id: str = "j-child") -> HarnessEvent:
    return pi_event(
        "meridian.subspawn.start",
        {
            "schema_version": 1,
            "subspawn_id": child_id,
            "correlation_id": child_id,
            "wait_policy": "tracked",
            "pid": 7701,
        },
    )


def _notification(event_type: str, notification_id: str = "n1") -> HarnessEvent:
    return pi_event(
        event_type,
        {
            "schema_version": 1,
            "notification_id": notification_id,
            "correlation_id": notification_id,
        },
    )


def _write_running_bash(runtime_root: Path, spawn_id: SpawnId) -> None:
    path = runtime_root / "pi-bash" / str(spawn_id) / "bash-records.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "records": {
                    "b1": {
                        "bash_id": "b1",
                        "is_tracked": True,
                        "is_background": True,
                        "status": "running",
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _start_row(
    runtime_root: Path,
    spawn_id: str,
    *,
    parent_id: str | None,
    status: SpawnStatus = "running",
) -> None:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=SpawnId(spawn_id),
        chat_id=spawn_id,
        parent_id=parent_id,
        model="test-model",
        agent="test-agent",
        harness=HarnessId.PI.value,
        prompt="hello",
        status=status,
    )


def _assert_failed(decision: DrainLoopDecision | DrainExitDecision, error: str) -> None:
    outcome = decision.recorded_outcome
    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert outcome.error == error


async def _execute_latched_cleanup(
    coordinator: PiDrainCoordinator,
    outcome: TerminalEventOutcome | None,
) -> None:
    exit_decision = await coordinator.handle_stream_exit(outcome)
    request = exit_decision.post_publication_cleanup
    assert request is not None
    await coordinator.execute_post_publication_cleanup(request)


@pytest.mark.asyncio
async def test_terminal_quiescent_starts_micro_drain_then_elapsed_candidate_finalizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(tmp_path, monkeypatch)
    coordinator = started.coordinator
    try:
        await coordinator.observe_event(_AGENT_END, "idle")
        terminal = await coordinator.handle_terminal_event(_AGENT_END, _SUCCESS, _TERMINATE)
        started.clock.advance(0.05)
        elapsed = await coordinator.handle_timeout()

        assert terminal.recorded_outcome is None
        assert elapsed.recorded_outcome == _SUCCESS
        assert [phase["phase"] for phase in started.phases].count(
            "quiescence_micro_drain_started"
        ) == 1
        assert started.cleanups == []
        assert started.nudges == []
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_stabilization_change_wins_over_done_then_done_finalizes_on_next_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(tmp_path, monkeypatch)
    coordinator = started.coordinator
    try:
        await coordinator.observe_event(_AGENT_END, "idle")
        await coordinator.handle_terminal_event(_AGENT_END, _SUCCESS, _TERMINATE)
        _start_row(tmp_path, "p2", parent_id="p1")
        write_spawn_signal(tmp_path, "p1", "done")
        started.clock.advance(0.05)

        stabilization = await coordinator.handle_timeout()
        done = await coordinator.handle_timeout()

        assert stabilization.recorded_outcome is None
        assert done.recorded_outcome == _SUCCESS
        assert any(
            phase["phase"] == "quiescence_micro_drain_cancelled"
            and phase["reason"] == "disk_state_changed"
            for phase in started.phases
        )
        assert started.cleanups == []
        assert started.nudges == []
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_done_override_wins_over_expired_child_wave_with_all_blockers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Current done override accepts success with bash, child, and notification blockers."""
    started = await _start_coordinator(
        tmp_path,
        monkeypatch,
        notification_timeout_seconds=10.0,
        child_wave_timeout_seconds=10.0,
    )
    coordinator = started.coordinator
    _write_running_bash(tmp_path, SpawnId("p1"))
    try:
        await coordinator.observe_event(_tracked_child_start(), None)
        await coordinator.observe_event(_notification("meridian.notification.queued"), None)
        await coordinator.observe_event(_AGENT_END, "idle")
        action = PiRpcQuiescenceDrainPolicy(
            quiescence_check=coordinator.is_quiescent
        ).classify(_SUCCESS)
        terminal = await coordinator.handle_terminal_event(_AGENT_END, _SUCCESS, action)
        write_spawn_signal(tmp_path, "p1", "done")
        started.clock.advance(10.0)

        decision = await coordinator.handle_timeout()

        assert terminal.recorded_outcome is None
        assert terminal.emit_turn_boundary is True
        assert decision.recorded_outcome == _SUCCESS
        assert started.cleanups == []
        assert started.nudges == []
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_done_fails_closed_when_pi_descendant_evidence_stays_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(
        tmp_path,
        monkeypatch,
    )
    coordinator = started.coordinator
    monkeypatch.setattr(
        pi_completion_profile_module,
        "PI_EVIDENCE_UNREADABLE_TIMEOUT_SECONDS",
        5.0,
    )

    def _fail_list_spawns(_runtime_root: Path) -> object:
        raise OSError("tree store unavailable")

    monkeypatch.setattr(
        descendant_evidence_module.spawn_store,
        "list_spawns",
        _fail_list_spawns,
    )
    try:
        await coordinator.observe_event(_AGENT_END, "idle")
        terminal = await coordinator.handle_terminal_event(
            _AGENT_END,
            _SUCCESS,
            _TERMINATE,
        )
        write_spawn_signal(tmp_path, "p1", "done")

        started.clock.advance(0.25)
        waiting = await coordinator.handle_timeout()

        assert terminal.recorded_outcome is None
        assert waiting.recorded_outcome is None

        started.clock.advance(5.0)
        expired = await coordinator.handle_timeout()

        _assert_failed(expired, "pi_evidence_unreadable")
        assert started.cleanups == []
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_done_completes_when_pi_descendant_evidence_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(
        tmp_path,
        monkeypatch,
        child_wave_timeout_seconds=5.0,
    )
    coordinator = started.coordinator
    store_available = False
    list_spawns = descendant_evidence_module.spawn_store.list_spawns

    def _sometimes_list_spawns(runtime_root: Path) -> object:
        if not store_available:
            raise OSError("tree store unavailable")
        return list_spawns(runtime_root)

    monkeypatch.setattr(
        descendant_evidence_module.spawn_store,
        "list_spawns",
        _sometimes_list_spawns,
    )
    try:
        await coordinator.observe_event(_AGENT_END, "idle")
        await coordinator.handle_terminal_event(_AGENT_END, _SUCCESS, _TERMINATE)
        write_spawn_signal(tmp_path, "p1", "done")
        waiting = await coordinator.handle_timeout()

        store_available = True
        started.clock.advance(0.25)
        recovered = await coordinator.handle_timeout()

        assert waiting.recorded_outcome is None
        assert recovered.recorded_outcome == _SUCCESS
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_done_fails_closed_on_pi_private_work_read_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(
        tmp_path,
        monkeypatch,
        child_wave_timeout_seconds=5.0,
    )
    coordinator = started.coordinator
    records_path = tmp_path / "pi-bash" / "p1" / "bash-records.json"
    records_path.write_text("{not json", encoding="utf-8")
    try:
        await coordinator.observe_event(_AGENT_END, "idle")
        terminal = await coordinator.handle_terminal_event(
            _AGENT_END,
            _SUCCESS,
            _TERMINATE,
        )
        write_spawn_signal(tmp_path, "p1", "done")
        waiting = await coordinator.handle_timeout()

        assert terminal.recorded_outcome is None
        assert waiting.recorded_outcome is None

        started.clock.advance(5.0)
        expired = await coordinator.handle_timeout()

        _assert_failed(expired, "pi_evidence_unreadable")
        assert started.cleanups == []
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_expired_notification_returns_existing_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(
        tmp_path,
        monkeypatch,
        notification_timeout_seconds=5.0,
    )
    coordinator = started.coordinator
    try:
        await coordinator.observe_event(_notification("meridian.notification.delivered"), None)
        started.clock.advance(5.0)

        decision = await coordinator.handle_timeout()

        _assert_failed(
            decision,
            "pi_notification_timeout:id=n1:phase=delivered:elapsed=5.000:timeout=5.000",
        )
        assert any(
            phase["phase"] == "pi_notification_timeout"
            and phase["notification_id"] == "n1"
            for phase in started.phases
        )
        assert started.cleanups == []
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_expired_notification_wins_over_expired_child_wave(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(
        tmp_path,
        monkeypatch,
        notification_timeout_seconds=5.0,
        child_wave_timeout_seconds=5.0,
    )
    coordinator = started.coordinator
    try:
        await coordinator.observe_event(_notification("meridian.notification.queued"), None)
        await coordinator.observe_event(_tracked_child_start(), None)
        await coordinator.observe_event(_AGENT_END, "idle")
        started.clock.advance(5.0)

        decision = await coordinator.handle_timeout()

        _assert_failed(
            decision,
            "pi_notification_timeout:id=n1:phase=queued:elapsed=5.000:timeout=5.000",
        )
        assert started.cleanups == []
        assert not any(
            phase["phase"] == "pi_child_wave_timeout" for phase in started.phases
        )
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_notification_timeout_preserves_configured_stream_exit_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(
        tmp_path,
        monkeypatch,
        notification_timeout_seconds=5.0,
    )
    coordinator = started.coordinator
    try:
        await coordinator.observe_event(_notification("meridian.notification.queued"), None)
        await coordinator.observe_event(_tracked_child_start(), None)
        started.clock.advance(5.0)

        timeout = await coordinator.handle_timeout()
        exit_decision = await coordinator.handle_stream_exit(timeout.recorded_outcome)
        request = exit_decision.post_publication_cleanup
        assert request is not None
        await coordinator.execute_post_publication_cleanup(request)

        _assert_failed(
            timeout,
            "pi_notification_timeout:id=n1:phase=queued:elapsed=5.000:timeout=5.000",
        )
        assert started.cleanups == ["pi_process_exit_with_tracked_children"]
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_expired_notification_wins_over_simultaneous_completion_nudge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(
        tmp_path,
        monkeypatch,
        notification_timeout_seconds=5.0,
        nudge_idle_seconds=5.0,
    )
    coordinator = started.coordinator
    _write_running_bash(tmp_path, SpawnId("p1"))
    try:
        await coordinator.observe_event(_notification("meridian.notification.queued"), None)
        await coordinator.observe_event(_AGENT_END, "idle")
        action = PiRpcQuiescenceDrainPolicy(
            quiescence_check=coordinator.is_quiescent
        ).classify(_SUCCESS)
        await coordinator.handle_terminal_event(_AGENT_END, _SUCCESS, action)
        started.clock.advance(5.0)

        decision = await coordinator.handle_timeout()

        _assert_failed(
            decision,
            "pi_notification_timeout:id=n1:phase=queued:elapsed=5.000:timeout=5.000",
        )
        assert started.nudges == []
        assert started.cleanups == []
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_expired_child_wave_wins_over_simultaneous_completion_nudge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(
        tmp_path,
        monkeypatch,
        child_wave_timeout_seconds=5.0,
        nudge_idle_seconds=5.0,
    )
    coordinator = started.coordinator
    try:
        await coordinator.observe_event(_tracked_child_start(), None)
        await coordinator.observe_event(_AGENT_END, "idle")
        action = PiRpcQuiescenceDrainPolicy(
            quiescence_check=coordinator.is_quiescent
        ).classify(_SUCCESS)
        await coordinator.handle_terminal_event(_AGENT_END, _SUCCESS, action)
        started.clock.advance(5.0)

        decision = await coordinator.handle_timeout()

        _assert_failed(decision, "pi_child_wave_timeout")
        assert started.cleanups == []
        await _execute_latched_cleanup(coordinator, decision.recorded_outcome)
        assert started.cleanups == ["pi_child_wave_timeout"]
        assert started.nudges == []
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_child_wave_timeout_without_cleanup_callback_preserves_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(
        tmp_path,
        monkeypatch,
        child_wave_timeout_seconds=5.0,
        cleanup_configured=False,
    )
    coordinator = started.coordinator
    try:
        await coordinator.observe_event(_tracked_child_start(), None)
        await coordinator.observe_event(_AGENT_END, "idle")
        started.clock.advance(5.0)

        decision = await coordinator.handle_timeout()

        _assert_failed(decision, "pi_child_wave_timeout")
        await _execute_latched_cleanup(coordinator, decision.recorded_outcome)
        assert started.cleanups == []
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_completion_nudge_due_is_advisory_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(
        tmp_path,
        monkeypatch,
        nudge_idle_seconds=5.0,
        nudge_raises=True,
    )
    coordinator = started.coordinator
    _write_running_bash(tmp_path, SpawnId("p1"))
    try:
        await coordinator.observe_event(_AGENT_END, "idle")
        action = PiRpcQuiescenceDrainPolicy(
            quiescence_check=coordinator.is_quiescent
        ).classify(_SUCCESS)
        terminal = await coordinator.handle_terminal_event(_AGENT_END, _SUCCESS, action)
        started.clock.advance(5.0)

        decision = await coordinator.handle_timeout()

        assert terminal.emit_turn_boundary is True
        assert decision.recorded_outcome is None
        assert started.nudges == [PI_COMPLETION_NUDGE_MESSAGE]
        assert started.cleanups == []
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_terminal_with_blockers_emits_quiescence_deferred(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(tmp_path, monkeypatch)
    coordinator = started.coordinator
    try:
        await coordinator.observe_event(_tracked_child_start(), None)
        await coordinator.observe_event(_AGENT_END, "idle")

        decision = await coordinator.handle_terminal_event(_AGENT_END, _SUCCESS, _TERMINATE)

        assert decision.recorded_outcome is None
        assert any(
            phase["phase"] == "quiescence_deferred"
            and phase["active_tracked_count"] == 1
            for phase in started.phases
        )
        assert not any(
            phase["phase"] == "quiescence_micro_drain_started"
            for phase in started.phases
        )
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_quiescence_deferred_categorizes_child_and_bash_blockers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _start_row(tmp_path, "p1", parent_id=None)
    _start_row(tmp_path, "p2", parent_id="p1")
    _write_running_bash(tmp_path, _SPAWN_ID)
    started = await _start_coordinator(tmp_path, monkeypatch)
    coordinator = started.coordinator
    try:
        await coordinator.observe_event(_tracked_child_start(), None)
        await coordinator.observe_event(_AGENT_END, "idle")

        await coordinator.handle_terminal_event(_AGENT_END, _SUCCESS, _TERMINATE)

        deferred = [
            phase for phase in started.phases if phase["phase"] == "quiescence_deferred"
        ]
        assert deferred[-1]["persisted_descendant_count"] == 1
        assert deferred[-1]["rowless_subspawn_count"] == 1
        assert deferred[-1]["tracked_bash_count"] == 1
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_after_event_notification_failure_prevents_new_stabilization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(tmp_path, monkeypatch)
    coordinator = started.coordinator
    try:
        await coordinator.observe_event(_AGENT_END, "idle")
        await coordinator.handle_terminal_event(_AGENT_END, _SUCCESS, _TERMINATE)
        coordinator.note_event_persisted(pi_event("message", {}))
        await coordinator.observe_event(
            _notification("meridian.notification.failed"),
            None,
        )

        decision = await coordinator.after_event()

        _assert_failed(decision, "pi_notification_failed")
        assert [phase["phase"] for phase in started.phases].count(
            "quiescence_micro_drain_started"
        ) == 1
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_after_event_cannot_replace_a_prior_terminal_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(
        tmp_path,
        monkeypatch,
        notification_timeout_seconds=5.0,
    )
    coordinator = started.coordinator
    try:
        await coordinator.observe_event(_AGENT_END, "idle")
        await coordinator.handle_terminal_event(_AGENT_END, _SUCCESS, _TERMINATE)
        coordinator.note_event_persisted(pi_event("message", {}))
        await coordinator.observe_event(_notification("meridian.notification.queued"), None)
        started.clock.advance(5.0)
        timeout = await coordinator.handle_timeout()
        _assert_failed(
            timeout,
            "pi_notification_timeout:id=n1:phase=queued:elapsed=5.000:timeout=5.000",
        )
        await coordinator.observe_event(
            _notification("meridian.notification.failed", "n2"),
            None,
        )

        decision = await coordinator.after_event()

        assert decision.recorded_outcome is None
        assert [phase["phase"] for phase in started.phases].count(
            "quiescence_micro_drain_started"
        ) == 1
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_note_event_persisted_extends_micro_drain_then_returns_lifecycle_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(tmp_path, monkeypatch)
    coordinator = started.coordinator
    persisted = pi_event("message", {})
    invalid = pi_event(
        "meridian.subspawn.start",
        {"schema_version": 999, "subspawn_id": "j-bad", "wait_policy": "tracked"},
    )
    try:
        await coordinator.observe_event(_AGENT_END, "idle")
        await coordinator.handle_terminal_event(_AGENT_END, _SUCCESS, _TERMINATE)
        await coordinator.observe_event(invalid, None)

        decision = coordinator.note_event_persisted(persisted)

        _assert_failed(
            decision,
            "pi_lifecycle_tracking_invalidated:unsupported_schema_version:999",
        )
        assert any(
            phase["phase"] == "quiescence_micro_drain_extended"
            and phase["event_type"] == "message"
            and phase["micro_drain_events"] == 1
            for phase in started.phases
        )
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_aux_blocker_retains_candidate_until_original_stabilization_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(tmp_path, monkeypatch)
    coordinator = started.coordinator
    try:
        await coordinator.observe_event(_AGENT_END, "idle")
        await coordinator.handle_terminal_event(_AGENT_END, _SUCCESS, _TERMINATE)

        started.clock.advance(0.02)
        _start_row(tmp_path, "p2", parent_id="p1")
        await coordinator.reevaluate_after_disk_change()

        assert coordinator.handle_close(intentional_stop=False) == _SUCCESS
        assert coordinator.next_timeout() == pytest.approx(0.03)
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_aux_blocker_cancels_at_original_timeout_then_completion_restarts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(tmp_path, monkeypatch)
    coordinator = started.coordinator
    try:
        await coordinator.observe_event(_AGENT_END, "idle")
        await coordinator.handle_terminal_event(_AGENT_END, _SUCCESS, _TERMINATE)

        started.clock.advance(0.02)
        _start_row(tmp_path, "p2", parent_id="p1")
        await coordinator.reevaluate_after_disk_change()

        started.clock.advance(0.03)
        blocked = await coordinator.handle_timeout()

        assert blocked.recorded_outcome is None
        assert coordinator.handle_close(intentional_stop=False) is None
        assert any(
            phase["phase"] == "quiescence_micro_drain_cancelled"
            and phase["reason"] == "disk_state_changed"
            for phase in started.phases
        )

        spawn_store.finalize_spawn(
            tmp_path,
            SpawnId("p2"),
            "succeeded",
            0,
            origin="runner",
        )
        await coordinator.reevaluate_after_disk_change()
        assert coordinator.handle_close(intentional_stop=False) == _SUCCESS
        started.clock.advance(0.05)

        decision = await coordinator.handle_timeout()

        assert decision.recorded_outcome == _SUCCESS
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_aux_blocker_that_clears_finalizes_at_original_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(tmp_path, monkeypatch)
    coordinator = started.coordinator
    try:
        await coordinator.observe_event(_AGENT_END, "idle")
        await coordinator.handle_terminal_event(_AGENT_END, _SUCCESS, _TERMINATE)

        started.clock.advance(0.02)
        _start_row(tmp_path, "p2", parent_id="p1")
        await coordinator.reevaluate_after_disk_change()
        started.clock.advance(0.01)
        spawn_store.finalize_spawn(
            tmp_path,
            SpawnId("p2"),
            "succeeded",
            0,
            origin="runner",
        )
        await coordinator.reevaluate_after_disk_change()

        assert coordinator.next_timeout() == pytest.approx(0.02)
        started.clock.advance(0.02)
        decision = await coordinator.handle_timeout()

        assert decision.recorded_outcome == _SUCCESS
        assert not any(
            phase["phase"] == "quiescence_micro_drain_cancelled"
            for phase in started.phases
        )
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_active_turn_before_expiry_resets_child_wave_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Current deadline reset grants a new full wave only for pre-expiry activity."""
    started = await _start_coordinator(
        tmp_path,
        monkeypatch,
        child_wave_timeout_seconds=10.0,
    )
    coordinator = started.coordinator
    try:
        await coordinator.observe_event(_tracked_child_start(), None)
        await coordinator.observe_event(_AGENT_END, "idle")
        started.clock.advance(5.0)
        await coordinator.observe_event(pi_event("agent_start", {}), "turn_active")
        started.clock.advance(4.0)
        await coordinator.observe_event(_AGENT_END, "idle")

        started.clock.advance(1.0)
        old_deadline = await coordinator.handle_timeout()
        started.clock.advance(9.0)
        reset_deadline = await coordinator.handle_timeout()

        assert old_deadline.recorded_outcome is None
        _assert_failed(reset_deadline, "pi_child_wave_timeout")
        assert started.cleanups == []
        await _execute_latched_cleanup(coordinator, reset_deadline.recorded_outcome)
        assert started.cleanups == ["pi_child_wave_timeout"]
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_activity_observed_after_child_wave_expiry_cannot_start_another_wave(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once expiry returns terminal, later activity cannot revive a full child wave."""
    started = await _start_coordinator(
        tmp_path,
        monkeypatch,
        child_wave_timeout_seconds=5.0,
    )
    coordinator = started.coordinator
    try:
        await coordinator.observe_event(_tracked_child_start(), None)
        await coordinator.observe_event(_AGENT_END, "idle")
        started.clock.advance(5.0)
        expired = await coordinator.handle_timeout()

        await coordinator.observe_event(pi_event("agent_start", {}), "turn_active")
        await coordinator.observe_event(_AGENT_END, "idle")
        started.clock.advance(5.0)
        after_activity = await coordinator.handle_timeout()

        _assert_failed(expired, "pi_child_wave_timeout")
        assert after_activity.recorded_outcome is None
        assert started.cleanups == []
        await _execute_latched_cleanup(coordinator, expired.recorded_outcome)
        assert started.cleanups == ["pi_child_wave_timeout"]
        assert [phase["phase"] for phase in started.phases].count(
            "pi_child_wave_timeout"
        ) == 1
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_stream_exit_with_pending_children_cleans_and_supplies_failure_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(tmp_path, monkeypatch)
    coordinator = started.coordinator
    try:
        await coordinator.observe_event(_tracked_child_start(), None)

        first = await coordinator.handle_stream_exit(None)
        second = await coordinator.handle_stream_exit(_SUCCESS)

        _assert_failed(first, "pi_process_exited_with_tracked_children")
        assert second.recorded_outcome == _SUCCESS
        assert started.cleanups == []
        request = first.post_publication_cleanup
        assert request is not None
        await coordinator.execute_post_publication_cleanup(request)
        assert started.cleanups == ["pi_process_exit_with_tracked_children"]
    finally:
        await coordinator.stop()
