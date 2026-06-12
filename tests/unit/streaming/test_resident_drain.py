from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.bootstrap.services import prepare_for_runtime_write
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.common import extract_codex_report
from meridian.lib.ops.spawn.models import SpawnSignalInput
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import LocalStore
from meridian.lib.streaming.drain_policy import (
    PersistentDrainPolicy,
)
from tests.support.fakes import FakeClock
from tests.unit.streaming.resident_drain_test_helpers import (
    FakeResidentConnection,
    awaiting_done_coordinator,
    coordinator_with_clock,
    next_turn_boundary,
    resident_event,
    start_manager,
    start_row,
)


@pytest.mark.asyncio
async def test_codex_terminal_success_without_live_children_finalizes_immediately(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p1")
    start_row(tmp_path, str(spawn_id), HarnessId.CODEX, None)
    connection = FakeResidentConnection(HarnessId.CODEX)
    manager = await start_manager(tmp_path, connection, spawn_id=spawn_id)

    connection.emit(resident_event(HarnessId.CODEX, "turn/completed", {}))

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=0.5)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert True not in connection.fake_resident_backend.awaiting_done_values
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_opencode_terminal_success_without_live_children_finalizes_immediately(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p1")
    start_row(tmp_path, str(spawn_id), HarnessId.OPENCODE, None)
    connection = FakeResidentConnection(HarnessId.OPENCODE)
    manager = await start_manager(tmp_path, connection, spawn_id=spawn_id)

    connection.emit(resident_event(HarnessId.OPENCODE, "session.idle", {}))

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=0.5)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert True not in connection.fake_resident_backend.awaiting_done_values
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_opencode_child_session_idle_does_not_finalize_parent(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p1")
    start_row(tmp_path, str(spawn_id), HarnessId.OPENCODE, None)
    connection = FakeResidentConnection(HarnessId.OPENCODE)
    manager = await start_manager(tmp_path, connection, spawn_id=spawn_id)
    subscriber = manager.subscribe(spawn_id)
    assert subscriber is not None

    child_idle = resident_event(
        HarnessId.OPENCODE,
        "session.idle",
        {"type": "session.idle", "properties": {"sessionID": "ses-child"}},
    )
    connection.emit(child_idle)

    try:
        observed = await asyncio.wait_for(subscriber.get(), timeout=0.5)
        assert observed == child_idle
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(manager.wait_for_completion(spawn_id)),
                timeout=0.05,
            )

        connection.emit(
            resident_event(
                HarnessId.OPENCODE,
                "session.idle",
                {"type": "session.idle", "properties": {"sessionID": "ses-resident"}},
            )
        )
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=0.5)
        assert outcome is not None
        assert outcome.status == "succeeded"
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_opencode_child_session_error_does_not_fail_parent(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p1")
    start_row(tmp_path, str(spawn_id), HarnessId.OPENCODE, None)
    connection = FakeResidentConnection(HarnessId.OPENCODE)
    manager = await start_manager(tmp_path, connection, spawn_id=spawn_id)

    connection.emit(
        resident_event(
            HarnessId.OPENCODE,
            "session.error",
            {"type": "session.error", "properties": {"sessionID": "ses-child"}},
        )
    )

    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(manager.wait_for_completion(spawn_id)),
                timeout=0.05,
            )

        connection.emit(
            resident_event(
                HarnessId.OPENCODE,
                "session.idle",
                {"type": "session.idle", "properties": {"sessionID": "ses-resident"}},
            )
        )
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=0.5)
        assert outcome is not None
        assert outcome.status == "succeeded"
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.parametrize(
    "harness_id,event_type",
    [
        (HarnessId.CODEX, "turn/completed"),
        (HarnessId.OPENCODE, "session.idle"),
    ],
)
@pytest.mark.asyncio
async def test_resident_persistent_policy_emits_boundary_and_stays_alive(
    tmp_path: Path,
    harness_id: HarnessId,
    event_type: str,
) -> None:
    spawn_id = SpawnId("p1")
    start_row(tmp_path, str(spawn_id), harness_id, None)
    connection = FakeResidentConnection(harness_id)
    manager = await start_manager(
        tmp_path,
        connection,
        spawn_id=spawn_id,
        drain_policy=PersistentDrainPolicy(),
    )
    subscriber = manager.subscribe(spawn_id)
    assert subscriber is not None

    connection.emit(resident_event(harness_id, event_type, {}))

    try:
        await next_turn_boundary(subscriber)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(manager.wait_for_completion(spawn_id)),
                timeout=0.05,
            )
        assert manager.get_connection(spawn_id) is connection
        assert True not in connection.fake_resident_backend.awaiting_done_values
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_opencode_terminal_success_resides_until_child_finishes(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p1")
    child_id = SpawnId("p2")
    start_row(tmp_path, str(spawn_id), HarnessId.OPENCODE, None)
    start_row(tmp_path, str(child_id), HarnessId.CODEX, str(spawn_id))
    connection = FakeResidentConnection(HarnessId.OPENCODE)
    manager = await start_manager(tmp_path, connection, spawn_id=spawn_id)

    connection.emit(resident_event(HarnessId.OPENCODE, "session.idle", {}))

    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(manager.wait_for_completion(spawn_id)),
                timeout=0.05,
            )
        assert manager.get_connection(spawn_id) is connection
        assert connection.fake_resident_backend.awaiting_done_values[-1] is True

        spawn_store.finalize_spawn(
            tmp_path,
            child_id,
            "succeeded",
            0,
            origin="runner",
        )
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=0.5)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert connection.fake_resident_backend.awaiting_done_values[-1] is False
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_resident_reconciles_finalizing_child_with_durable_report_as_done(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p1")
    child_id = SpawnId("p2")
    start_row(tmp_path, str(spawn_id), HarnessId.OPENCODE, None)
    start_row(tmp_path, str(child_id), HarnessId.CODEX, str(spawn_id))
    spawn_store.mark_finalizing(tmp_path, child_id)
    (tmp_path / "spawns" / str(child_id) / "report.md").write_text(
        "# Report\n\nChild completed.\n",
        encoding="utf-8",
    )
    connection = FakeResidentConnection(HarnessId.OPENCODE)
    manager = await start_manager(tmp_path, connection, spawn_id=spawn_id)

    connection.emit(resident_event(HarnessId.OPENCODE, "session.idle", {}))

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=0.5)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert True not in connection.fake_resident_backend.awaiting_done_values
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_resident_still_waits_on_genuinely_active_finalizing_child(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p1")
    child_id = SpawnId("p2")
    start_row(tmp_path, str(spawn_id), HarnessId.OPENCODE, None)
    start_row(tmp_path, str(child_id), HarnessId.CODEX, str(spawn_id))
    spawn_store.mark_finalizing(tmp_path, child_id)
    connection = FakeResidentConnection(HarnessId.OPENCODE)
    manager = await start_manager(tmp_path, connection, spawn_id=spawn_id)

    connection.emit(resident_event(HarnessId.OPENCODE, "session.idle", {}))

    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(manager.wait_for_completion(spawn_id)),
                timeout=0.05,
            )
        assert manager.get_connection(spawn_id) is connection
        assert connection.fake_resident_backend.awaiting_done_values[-1] is True
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_done_signal_at_terminalresident_event_wins_over_outstanding_child(
    tmp_path: Path,
) -> None:
    from meridian.lib.state.spawn_signals import write_spawn_signal

    spawn_id = SpawnId("p1")
    start_row(tmp_path, str(spawn_id), HarnessId.OPENCODE, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, str(spawn_id))
    write_spawn_signal(tmp_path, spawn_id, "done")
    write_spawn_signal(tmp_path, spawn_id, "rearm")
    connection = FakeResidentConnection(HarnessId.OPENCODE)
    manager = await start_manager(tmp_path, connection, spawn_id=spawn_id)

    connection.emit(resident_event(HarnessId.OPENCODE, "session.idle", {}))

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=0.5)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert True not in connection.fake_resident_backend.awaiting_done_values
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_done_op_releases_resident_wait_via_environment_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    prepared = prepare_for_runtime_write(project_root)
    runtime_root = prepared.runtime_root
    assert runtime_root is not None
    spawn_id = SpawnId("p1")
    start_row(runtime_root, str(spawn_id), HarnessId.OPENCODE, None)
    start_row(runtime_root, "p2", HarnessId.CODEX, str(spawn_id))
    connection = FakeResidentConnection(HarnessId.OPENCODE)
    manager = await start_manager(
        runtime_root,
        connection,
        spawn_id=spawn_id,
        project_root=project_root,
    )

    connection.emit(resident_event(HarnessId.OPENCODE, "session.idle", {}))
    completion_task = asyncio.create_task(manager.wait_for_completion(spawn_id))

    try:
        await asyncio.sleep(0.03)
        assert not completion_task.done()
        monkeypatch.setenv("MERIDIAN_SPAWN_ID", str(spawn_id))

        result = spawn_api.spawn_done_sync(SpawnSignalInput(), prepared=prepared)
        outcome = await asyncio.wait_for(completion_task, timeout=0.5)

        assert result.status == "succeeded"
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert connection.fake_resident_backend.awaiting_done_values[-1] is False
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_rearm_op_extends_resident_deadline(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    prepared = prepare_for_runtime_write(project_root)
    runtime_root = prepared.runtime_root
    assert runtime_root is not None
    spawn_id = SpawnId("p1")
    start_row(runtime_root, str(spawn_id), HarnessId.CODEX, None)
    start_row(runtime_root, "p2", HarnessId.CODEX, str(spawn_id))
    connection = FakeResidentConnection(HarnessId.CODEX)
    manager = await start_manager(
        runtime_root,
        connection,
        spawn_id=spawn_id,
        project_root=project_root,
        resident_deadline_seconds=0.2,
        resident_poll_seconds=0.01,
    )

    connection.emit(resident_event(HarnessId.CODEX, "turn/completed", {}))
    completion_task = asyncio.create_task(manager.wait_for_completion(spawn_id))

    try:
        await asyncio.sleep(0.1)
        assert not completion_task.done()

        result = spawn_api.spawn_rearm_sync(
            SpawnSignalInput(spawn_id=str(spawn_id)),
            ctx=RuntimeContext(spawn_id=spawn_id),
            prepared=prepared,
        )
        await asyncio.sleep(0.12)

        assert result.status == "succeeded"
        assert not completion_task.done()

        done_result = spawn_api.spawn_done_sync(
            SpawnSignalInput(spawn_id=str(spawn_id)),
            prepared=prepared,
        )
        outcome = await asyncio.wait_for(completion_task, timeout=0.5)
        assert done_result.status == "succeeded"
        assert outcome is not None
        assert outcome.status == "succeeded"
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_resident_wait_fans_out_turn_boundary_to_subscriber(tmp_path: Path) -> None:
    spawn_id = SpawnId("p1")
    start_row(tmp_path, str(spawn_id), HarnessId.CODEX, None)
    start_row(tmp_path, "p2", HarnessId.OPENCODE, str(spawn_id))
    connection = FakeResidentConnection(HarnessId.CODEX)
    manager = await start_manager(tmp_path, connection, spawn_id=spawn_id)
    subscriber = manager.subscribe(spawn_id)
    assert subscriber is not None

    connection.emit(resident_event(HarnessId.CODEX, "turn/completed", {}))

    try:
        boundary = await next_turn_boundary(subscriber)
        assert boundary.payload["status"] == "succeeded"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(manager.wait_for_completion(spawn_id)),
                timeout=0.05,
            )
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_child_written_before_terminalresident_event_is_processed_prevents_early_finalize(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p1")
    child_id = SpawnId("p2")
    start_row(tmp_path, str(spawn_id), HarnessId.CODEX, None)
    connection = FakeResidentConnection(HarnessId.CODEX)
    manager = await start_manager(tmp_path, connection, spawn_id=spawn_id)

    connection.emit(resident_event(HarnessId.CODEX, "turn/completed", {}))
    start_row(tmp_path, str(child_id), HarnessId.OPENCODE, str(spawn_id))

    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(manager.wait_for_completion(spawn_id)),
                timeout=0.05,
            )
        spawn_store.finalize_spawn(tmp_path, child_id, "succeeded", 0, origin="runner")
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=0.5)
        assert outcome is not None
        assert outcome.status == "succeeded"
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_resident_stream_close_with_dead_backend_fails_while_child_running(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p1")
    start_row(tmp_path, str(spawn_id), HarnessId.OPENCODE, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, str(spawn_id))
    connection = FakeResidentConnection(HarnessId.OPENCODE)
    manager = await start_manager(tmp_path, connection, spawn_id=spawn_id)

    connection.emit(resident_event(HarnessId.OPENCODE, "session.idle", {}))

    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(manager.wait_for_completion(spawn_id)),
                timeout=0.05,
            )
        connection.mark_failed()
        connection.close_stream()

        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=0.5)
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.error == "backend_dead_while_awaiting_done"
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_resident_stream_close_with_stalled_backend_is_not_dead_outcome(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p1")
    start_row(tmp_path, str(spawn_id), HarnessId.OPENCODE, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, str(spawn_id))
    connection = FakeResidentConnection(HarnessId.OPENCODE)
    manager = await start_manager(tmp_path, connection, spawn_id=spawn_id)

    connection.emit(resident_event(HarnessId.OPENCODE, "session.idle", {}))

    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(manager.wait_for_completion(spawn_id)),
                timeout=0.05,
            )
        connection.mark_stalled()
        connection.close_stream()

        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=0.5)
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.error == "stream_closed_while_awaiting_done"
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_codex_resident_deadline_waits_then_reaps_live_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_id = SpawnId("p1")
    start_row(tmp_path, str(spawn_id), HarnessId.CODEX, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, str(spawn_id))
    from meridian.lib.bootstrap import services as bootstrap_services
    from meridian.lib.state.spawn_tree import active_descendants

    reaped_spawn_ids: list[str] = []

    class _FakeService:
        async def cancel_descendants(self, root_id: SpawnId) -> set[str]:
            reaped_ids: set[str] = set()
            for descendant in active_descendants(tmp_path, root_id):
                reaped_ids.add(descendant.id)
                reaped_spawn_ids.append(descendant.id)
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
    connection = FakeResidentConnection(HarnessId.CODEX)
    manager = await start_manager(
        tmp_path,
        connection,
        spawn_id=spawn_id,
        resident_deadline_seconds=0.08,
        resident_poll_seconds=0.01,
    )

    connection.emit(resident_event(HarnessId.CODEX, "turn/completed", {}))
    completion_task = asyncio.create_task(manager.wait_for_completion(spawn_id))

    try:
        await asyncio.sleep(0.03)
        assert not completion_task.done()
        assert connection.fake_resident_backend.awaiting_done_values[-1] is True

        outcome = await asyncio.wait_for(completion_task, timeout=0.5)
        assert outcome is not None
        assert outcome.status == "timed_out"
        assert outcome.error == "resident_deadline_expired"
        assert reaped_spawn_ids == ["p2"]
        child = spawn_store.get_spawn(tmp_path, SpawnId("p2"))
        assert child is not None
        assert child.status == "cancelled"
        assert connection.fake_resident_backend.awaiting_done_values[-1] is False
    finally:
        await manager.stop_spawn(spawn_id)

@pytest.mark.asyncio
async def test_codex_resident_finalization_preserves_artifact_report(tmp_path: Path) -> None:
    spawn_id = SpawnId("p1")
    child_id = SpawnId("p2")
    start_row(tmp_path, str(spawn_id), HarnessId.CODEX, None)
    start_row(tmp_path, str(child_id), HarnessId.OPENCODE, str(spawn_id))
    connection = FakeResidentConnection(HarnessId.CODEX)
    manager = await start_manager(tmp_path, connection, spawn_id=spawn_id)

    connection.emit(
        resident_event(
            HarnessId.CODEX,
            "item/completed",
            {"item": {"type": "agentMessage", "text": "Resident report."}},
        )
    )
    connection.emit(resident_event(HarnessId.CODEX, "turn/completed", {}))

    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(manager.wait_for_completion(spawn_id)),
                timeout=0.05,
            )
        spawn_store.finalize_spawn(
            tmp_path,
            child_id,
            "succeeded",
            0,
            origin="runner",
        )
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=0.5)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert extract_codex_report(LocalStore(root_dir=tmp_path / "spawns"), spawn_id) == (
            "Resident report."
        )
    finally:
        await manager.stop_spawn(spawn_id)




@pytest.mark.asyncio
async def test_rearm_signal_extends_deadline_and_injects_only_after_cadence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.state.spawn_signals import write_spawn_signal
    from meridian.lib.streaming import resident_drain as resident_drain_module

    clock = FakeClock(start=100.0)
    monkeypatch.setattr(resident_drain_module.time, "monotonic", clock.monotonic)
    start_row(tmp_path, "p1", HarnessId.CODEX, None)
    connection = FakeResidentConnection(HarnessId.CODEX)
    coordinator = coordinator_with_clock(tmp_path, connection, clock, deadline_seconds=3300.0)

    clock.advance(10.0)
    write_spawn_signal(tmp_path, "p1", "rearm")
    old_deadline = coordinator.deadline_monotonic
    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is None
    assert coordinator.resident_requested is True
    assert old_deadline is not None
    assert coordinator.deadline_monotonic is not None
    assert coordinator.deadline_monotonic > old_deadline
    assert connection.fake_resident_backend.injected_messages == []
    assert coordinator.next_timeout() == pytest.approx(5.0)

    clock.advance(270.0)
    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is None
    assert len(connection.fake_resident_backend.injected_messages) == 1
    injected = connection.fake_resident_backend.injected_messages[0]
    assert "meridian spawn done" in injected
    assert "meridian spawn rearm" in injected

    write_spawn_signal(tmp_path, "p1", "done")
    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is not None
    assert decision.recorded_outcome.status == "succeeded"
    assert coordinator.pending_outcome is None


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
    connection = FakeResidentConnection(HarnessId.CODEX)
    coordinator = coordinator_with_clock(tmp_path, connection, clock, deadline_seconds=300.0)
    coordinator.resident_requested = True
    coordinator.next_inject_monotonic = clock.monotonic()

    coordinator.observe_activity_transition("turn_active")

    assert coordinator.turn_active is True
    assert coordinator.pending_outcome is not None
    assert coordinator.deadline_monotonic is not None
    assert coordinator.deadline_monotonic > clock.monotonic()
    assert coordinator.next_inject_monotonic is not None
    assert coordinator.next_inject_monotonic > clock.monotonic()
    assert coordinator.next_timeout() == pytest.approx(5.0)

    write_spawn_signal(tmp_path, "p1", "done")
    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is not None
    assert decision.recorded_outcome.status == "succeeded"
    assert coordinator.deadline_monotonic is None


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
    coordinator = coordinator_with_clock(tmp_path, connection, clock, deadline_seconds=10.0)
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
    coordinator = coordinator_with_clock(tmp_path, connection, clock, deadline_seconds=10.0)

    class _FailingService:
        async def cancel_descendants(self, root_id: SpawnId) -> set[str]:
            _ = root_id
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
    coordinator = coordinator_with_clock(tmp_path, connection, clock, deadline_seconds=10.0)
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
    coordinator = awaiting_done_coordinator(tmp_path, connection)

    write_spawn_signal(tmp_path, "p1", "done")
    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is not None
    assert decision.recorded_outcome.status == "succeeded"
    assert connection.fake_resident_backend.injected_messages == []
