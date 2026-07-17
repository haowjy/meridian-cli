from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.semantics import TerminalEventOutcome
from meridian.lib.state import spawn_store
from meridian.lib.streaming import descendant_evidence as descendant_evidence_module
from meridian.lib.streaming.drain_policy import (
    DrainAction,
)
from meridian.lib.streaming.resident_drain import ResidentDrainCoordinator
from tests.support.fakes import FakeClock
from tests.support.pi import start_row
from tests.support.resident_drain import (
    FakeResidentConnection,
    awaiting_done_coordinator,
    coordinator_with_clock,
    descendant_cancellation_from_roots,
    resident_event,
)


async def _execute_latched_cleanup(
    coordinator: ResidentDrainCoordinator,
    outcome: TerminalEventOutcome | None,
) -> None:
    exit_decision = await coordinator.handle_stream_exit(outcome)
    request = exit_decision.post_publication_cleanup
    assert request is not None
    await coordinator.execute_post_publication_cleanup(request)

@pytest.mark.asyncio
async def test_terminal_done_fails_closed_when_descendant_evidence_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.state.spawn_signals import write_spawn_signal
    from meridian.lib.streaming import resident_drain as resident_drain_module

    start_row(tmp_path, "p1", HarnessId.CODEX, None)
    clock = FakeClock(start=100.0)
    monkeypatch.setattr(resident_drain_module.time, "monotonic", clock.monotonic)
    connection = FakeResidentConnection(HarnessId.CODEX)
    coordinator = ResidentDrainCoordinator.for_connection(
        runtime_root=tmp_path,
        spawn_id=SpawnId("p1"),
        receiver=connection,
        resident_backend=connection.resident_backend,
        deadline_seconds=30.0,
        poll_seconds=0.01,
        rearm_budget=None,
        cancel_descendants=descendant_cancellation_from_roots(tmp_path, tmp_path),
    )
    write_spawn_signal(tmp_path, "p1", "done")

    def _raise_evidence_read_failure(*_args: object) -> None:
        raise OSError("descendant evidence unavailable")

    monkeypatch.setattr(
        descendant_evidence_module.spawn_store,
        "list_spawns",
        _raise_evidence_read_failure,
    )
    terminal = TerminalEventOutcome(status="succeeded", exit_code=0)

    decision = await coordinator.handle_terminal_event(
        resident_event(HarnessId.CODEX, "turn/completed", {}),
        terminal,
        DrainAction(terminate=True, emit_turn_boundary=False),
    )

    assert decision.recorded_outcome is None
    assert decision.emit_turn_boundary is True
    assert connection.fake_resident_backend.awaiting_done_values == [True]

    clock.advance(30.0)
    expired = await coordinator.handle_timeout()

    assert expired.recorded_outcome is not None
    assert expired.recorded_outcome.status == "failed"
    assert expired.recorded_outcome.exit_code == 1
    assert expired.recorded_outcome.error == "resident_evidence_unreadable"

@pytest.mark.asyncio
async def test_terminal_done_completes_when_descendant_evidence_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.state.spawn_signals import write_spawn_signal
    from meridian.lib.streaming import resident_drain as resident_drain_module

    clock = FakeClock(start=100.0)
    monkeypatch.setattr(resident_drain_module.time, "monotonic", clock.monotonic)
    start_row(tmp_path, "p1", HarnessId.CODEX, None)
    connection = FakeResidentConnection(HarnessId.CODEX)
    coordinator = ResidentDrainCoordinator.for_connection(
        runtime_root=tmp_path,
        spawn_id=SpawnId("p1"),
        receiver=connection,
        resident_backend=connection.resident_backend,
        deadline_seconds=30.0,
        poll_seconds=0.01,
        rearm_budget=None,
        cancel_descendants=descendant_cancellation_from_roots(tmp_path, tmp_path),
    )
    evidence_readable = False
    list_spawns = descendant_evidence_module.spawn_store.list_spawns

    def _read_descendants(runtime_root: Path) -> object:
        if not evidence_readable:
            raise OSError("descendant evidence unavailable")
        return list_spawns(runtime_root)

    monkeypatch.setattr(
        descendant_evidence_module.spawn_store,
        "list_spawns",
        _read_descendants,
    )
    write_spawn_signal(tmp_path, "p1", "done")
    terminal = TerminalEventOutcome(status="succeeded", exit_code=0)

    waiting = await coordinator.handle_terminal_event(
        resident_event(HarnessId.CODEX, "turn/completed", {}),
        terminal,
        DrainAction(terminate=True, emit_turn_boundary=False),
    )
    evidence_readable = True
    recovered = await coordinator.handle_timeout()

    assert waiting.recorded_outcome is None
    assert recovered.recorded_outcome == terminal

@pytest.mark.asyncio
async def test_active_followup_turn_stays_resident_honors_done_and_defers_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.state.spawn_signals import write_spawn_signal
    from meridian.lib.streaming import resident_drain as resident_drain_module

    clock = FakeClock(start=100.0)
    monkeypatch.setattr(resident_drain_module.time, "monotonic", clock.monotonic)
    start_row(tmp_path, "p1", HarnessId.CODEX, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    connection = FakeResidentConnection(HarnessId.CODEX)
    coordinator = ResidentDrainCoordinator.for_connection(
        runtime_root=tmp_path,
        spawn_id=SpawnId("p1"),
        receiver=connection,
        resident_backend=connection.resident_backend,
        deadline_seconds=3300.0,
        poll_seconds=5.0,
        rearm_budget=None,
        cancel_descendants=descendant_cancellation_from_roots(tmp_path, tmp_path),
    )
    terminal = TerminalEventOutcome(status="succeeded", exit_code=0)
    waiting = await coordinator.handle_terminal_event(
        resident_event(HarnessId.CODEX, "turn/completed", {}),
        terminal,
        DrainAction(terminate=True, emit_turn_boundary=False),
    )
    assert waiting.emit_turn_boundary is True
    spawn_store.finalize_spawn(tmp_path, SpawnId("p2"), "succeeded", 0, origin="runner")
    write_spawn_signal(tmp_path, "p1", "rearm")
    rearmed = await coordinator.handle_timeout()
    assert rearmed.recorded_outcome is None
    assert coordinator.next_timeout() == pytest.approx(5.0)
    assert connection.fake_resident_backend.injected_messages == []

    clock.advance(270.0)
    nudged = await coordinator.handle_timeout()
    assert nudged.recorded_outcome is None
    assert len(connection.fake_resident_backend.injected_messages) == 1
    injected = connection.fake_resident_backend.injected_messages[0]
    assert "meridian spawn done" in injected
    assert "meridian spawn rearm" in injected

    coordinator.observe_activity_transition("turn_active")

    assert coordinator.next_timeout() == pytest.approx(5.0)
    assert connection.fake_resident_backend.awaiting_done_values == [True, False]

    write_spawn_signal(tmp_path, "p1", "done")
    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome == terminal
    assert coordinator.next_timeout() is None
    assert connection.fake_resident_backend.awaiting_done_values == [True, False, False]

@pytest.mark.asyncio
async def test_active_followup_turn_still_enforces_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.streaming import resident_drain as resident_drain_module

    clock = FakeClock(start=0.0)
    monkeypatch.setattr(resident_drain_module.time, "monotonic", clock.monotonic)
    start_row(tmp_path, "p1", HarnessId.CODEX, None)
    connection = FakeResidentConnection(HarnessId.CODEX)
    coordinator = await coordinator_with_clock(tmp_path, connection, clock, deadline_seconds=10.0)
    coordinator.observe_activity_transition("turn_active")

    clock.advance(10.0)
    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is not None
    assert decision.recorded_outcome.status == "timed_out"
    assert decision.recorded_outcome.error == "resident_deadline_expired"

@pytest.mark.asyncio
async def test_deadline_returns_timed_out_when_descendant_reap_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.bootstrap import services as bootstrap_services
    from meridian.lib.streaming import resident_drain as resident_drain_module

    clock = FakeClock(start=0.0)
    monkeypatch.setattr(resident_drain_module.time, "monotonic", clock.monotonic)
    start_row(tmp_path, "p1", HarnessId.CODEX, None)
    connection = FakeResidentConnection(HarnessId.CODEX)
    coordinator = await coordinator_with_clock(tmp_path, connection, clock, deadline_seconds=10.0)
    cleanup_calls = 0

    class _FailingService:
        async def cancel_descendants(self, root_id: SpawnId) -> set[str]:
            nonlocal cleanup_calls
            _ = root_id
            cleanup_calls += 1
            raise RuntimeError("teardown failed")

    monkeypatch.setattr(
        bootstrap_services,
        "build_spawn_application_service_from_roots",
        lambda _project_root, _runtime_root: _FailingService(),
    )
    clock.advance(10.0)

    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is not None
    assert decision.recorded_outcome.status == "timed_out"
    assert decision.recorded_outcome.error == "resident_deadline_expired"
    assert cleanup_calls == 0
    await _execute_latched_cleanup(coordinator, decision.recorded_outcome)
    assert cleanup_calls == 1

    later_decision = await coordinator.handle_timeout()
    assert later_decision.recorded_outcome is None
    assert cleanup_calls == 1

@pytest.mark.asyncio
async def test_deadline_reap_cancels_active_descendant_cli_spawns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.bootstrap import services as bootstrap_services
    from meridian.lib.state.spawn_tree import active_descendants
    from meridian.lib.streaming import resident_drain as resident_drain_module

    clock = FakeClock(start=0.0)
    monkeypatch.setattr(resident_drain_module.time, "monotonic", clock.monotonic)
    start_row(tmp_path, "p1", HarnessId.CODEX, None)
    start_row(tmp_path, "p2", HarnessId.OPENCODE, "p1")
    start_row(tmp_path, "p3", HarnessId.OPENCODE, "p1")
    spawn_store.finalize_spawn(tmp_path, SpawnId("p3"), "succeeded", 0, origin="runner")
    connection = FakeResidentConnection(HarnessId.CODEX)
    coordinator = await coordinator_with_clock(tmp_path, connection, clock, deadline_seconds=10.0)
    cancelled: list[str] = []

    class _FakeService:
        async def cancel_descendants(self, root_id: SpawnId) -> set[str]:
            reaped_ids: set[str] = set()
            for descendant in active_descendants(tmp_path, root_id):
                reaped_ids.add(descendant.id)
                cancelled.append(descendant.id)
                spawn_store.finalize_spawn(
                    tmp_path,
                    descendant.id,
                    "cancelled",
                    130,
                    origin="cancel",
                    error="cancelled",
                )
            return reaped_ids

    monkeypatch.setattr(
        bootstrap_services,
        "build_spawn_application_service_from_roots",
        lambda _project_root, _runtime_root: _FakeService(),
    )
    clock.advance(10.0)

    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is not None
    assert decision.recorded_outcome.status == "timed_out"
    assert cancelled == []
    await _execute_latched_cleanup(coordinator, decision.recorded_outcome)
    assert cancelled == ["p2"]
    child = spawn_store.get_spawn(tmp_path, SpawnId("p2"))
    assert child is not None
    assert child.status == "cancelled"
    already_terminal = spawn_store.get_spawn(tmp_path, SpawnId("p3"))
    assert already_terminal is not None
    assert already_terminal.status == "succeeded"

@pytest.mark.asyncio
async def test_done_signal_is_honored_with_tracked_child_outstanding(
    tmp_path: Path,
) -> None:
    from meridian.lib.state.spawn_signals import write_spawn_signal

    start_row(tmp_path, "p1", HarnessId.OPENCODE, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    connection = FakeResidentConnection(HarnessId.OPENCODE)
    coordinator = await awaiting_done_coordinator(tmp_path, connection)

    write_spawn_signal(tmp_path, "p1", "done")
    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is not None
    assert decision.recorded_outcome.status == "succeeded"
    assert connection.fake_resident_backend.injected_messages == []
