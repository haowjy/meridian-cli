"""Fake-clock characterization of Pi drain coordination and current authority."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.harness.semantics import TerminalEventOutcome
from meridian.lib.state.spawn_signals import write_spawn_signal
from meridian.lib.streaming import descendant_evidence as descendant_evidence_module
from meridian.lib.streaming import pi_completion_profile as pi_completion_profile_module
from meridian.lib.streaming.completion_nudge import PI_COMPLETION_NUDGE_MESSAGE
from meridian.lib.streaming.drain_coordinator import DrainExitDecision, DrainLoopDecision
from meridian.lib.streaming.drain_policy import DrainAction, PiRpcQuiescenceDrainPolicy
from meridian.lib.streaming.pi_drain import PiDrainCoordinator
from tests.support.pi import PiDrainScenario, pi_event, start_row

_SPAWN_ID = SpawnId("p1")
_SUCCESS = TerminalEventOutcome(status="succeeded", exit_code=0)
_TERMINATE = DrainAction(terminate=True, emit_turn_boundary=False)
_AGENT_END = pi_event("agent_end")
_start_coordinator = PiDrainScenario.start


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
    # Scenario owns the disk shape; this wrapper preserves descriptive call sites.
    scenario_path = runtime_root / "pi-bash" / str(spawn_id) / "bash-records.json"
    scenario_path.parent.mkdir(parents=True, exist_ok=True)
    scenario_path.write_text(
        '{"records":{"b1":{"bash_id":"b1","is_tracked":true,"is_background":true,"status":"running"}}}',
        encoding="utf-8",
    )


def _start_row(
    runtime_root: Path, spawn_id: str, *, parent_id: str | None, status: SpawnStatus = "running"
) -> None:
    start_row(runtime_root, spawn_id, HarnessId.CODEX, parent_id, status=status)


def _assert_failed(decision: DrainLoopDecision | DrainExitDecision, error: str) -> None:
    outcome = decision.recorded_outcome
    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert outcome.error == error


async def _execute_latched_cleanup(
    coordinator: PiDrainCoordinator, outcome: TerminalEventOutcome | None
) -> None:
    exit_decision = await coordinator.handle_stream_exit(outcome)
    request = exit_decision.post_publication_cleanup
    assert request is not None
    await coordinator.execute_post_publication_cleanup(request)


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

        started.clock.advance(4.999)
        just_before_deadline = await coordinator.handle_timeout()

        assert just_before_deadline.recorded_outcome is None

        started.clock.advance(0.001)
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
            phase["phase"] == "pi_notification_timeout" and phase["notification_id"] == "n1"
            for phase in started.phases
        )
        assert started.cleanups == []
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
        _assert_failed(
            exit_decision,
            "pi_notification_timeout:id=n1:phase=queued:elapsed=5.000:timeout=5.000",
        )
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
        action = PiRpcQuiescenceDrainPolicy(quiescence_check=coordinator.is_quiescent).classify(
            _SUCCESS
        )
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
