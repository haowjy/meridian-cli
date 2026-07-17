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
from tests.support.async_determinism import (
    AsyncDeterminism,
    assert_still_pending,
    wait_until,
)
from tests.support.pi import start_row
from tests.support.resident_drain import (
    FakeResidentConnection,
    next_turn_boundary,
    resident_event,
    start_manager,
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
        outcome = await manager.wait_for_completion(spawn_id)
        assert outcome is not None
        assert outcome.status == "succeeded"
    finally:
        await manager.stop_spawn(spawn_id)

@pytest.mark.asyncio
async def test_codex_backend_death_with_pending_success_preserves_resident_reason(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p1")
    start_row(tmp_path, str(spawn_id), HarnessId.CODEX, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, str(spawn_id))
    connection = FakeResidentConnection(HarnessId.CODEX)
    manager = await start_manager(tmp_path, connection, spawn_id=spawn_id)
    subscriber = manager.subscribe(spawn_id)
    assert subscriber is not None

    connection.emit(resident_event(HarnessId.CODEX, "turn/completed", {}))
    await next_turn_boundary(subscriber)
    connection.fail_backend("no close frame received or sent")

    try:
        outcome = await manager.wait_for_completion(spawn_id)
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.error == "backend_dead_while_awaiting_done"
    finally:
        await manager.stop_spawn(spawn_id)

@pytest.mark.asyncio
async def test_opencode_process_death_with_pending_success_preserves_exit_diagnostics(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p1")
    start_row(tmp_path, str(spawn_id), HarnessId.OPENCODE, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, str(spawn_id))
    connection = FakeResidentConnection(HarnessId.OPENCODE)
    manager = await start_manager(tmp_path, connection, spawn_id=spawn_id)
    subscriber = manager.subscribe(spawn_id)
    assert subscriber is not None

    connection.emit(resident_event(HarnessId.OPENCODE, "session.idle", {}))
    await next_turn_boundary(subscriber)
    diagnostic = (
        "OpenCode subprocess exited with code 17.\n\n"
        "OpenCode subprocess stderr:\n"
        "fatal backend detail"
    )
    connection.fail_backend(diagnostic)

    try:
        outcome = await manager.wait_for_completion(spawn_id)
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.error == diagnostic
        assert outcome.error != "backend_dead_while_awaiting_done"
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
        outcome = await manager.wait_for_completion(spawn_id)
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
        observed = await subscriber.get()
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
        outcome = await manager.wait_for_completion(spawn_id)
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
        outcome = await manager.wait_for_completion(spawn_id)
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
        outcome = await manager.wait_for_completion(spawn_id)
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
        outcome = await manager.wait_for_completion(spawn_id)
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
        outcome = await manager.wait_for_completion(spawn_id)
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
        await assert_still_pending(completion_task)
        monkeypatch.setenv("MERIDIAN_SPAWN_ID", str(spawn_id))

        result = spawn_api.spawn_done_sync(SpawnSignalInput(), prepared=prepared)
        outcome = await completion_task

        assert result.status == "succeeded"
        assert outcome is not None
        assert outcome.status == "succeeded"
    finally:
        await manager.stop_spawn(spawn_id)

@pytest.mark.asyncio
async def test_spawn_rearm_op_extends_resident_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.bootstrap import services as bootstrap_services
    from meridian.lib.state.spawn_tree import active_descendants
    from meridian.lib.streaming import resident_drain as resident_drain_module

    determinism = AsyncDeterminism(start=100.0)
    determinism.install(monkeypatch, monotonic_modules=(resident_drain_module,))
    determinism.install_on_running_loop(monkeypatch)

    reaped_spawn_ids: list[str] = []

    class _FakeService:
        async def cancel_descendants(self, root_id: SpawnId) -> set[str]:
            reaped_ids: set[str] = set()
            for descendant in active_descendants(runtime_root, root_id):
                reaped_ids.add(descendant.id)
                reaped_spawn_ids.append(descendant.id)
                spawn_store.finalize_spawn(
                    runtime_root,
                    descendant.id,
                    "cancelled",
                    130,
                    origin="cancel",
                    error="cancelled",
                )
            return reaped_ids

    project_root = tmp_path / "repo"
    project_root.mkdir()
    prepared = prepare_for_runtime_write(project_root)
    runtime_root = prepared.runtime_root
    assert runtime_root is not None

    monkeypatch.setattr(
        bootstrap_services,
        "build_spawn_application_service_from_roots",
        lambda _project_root, _runtime_root: _FakeService(),
    )
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
        await wait_until(
            lambda: connection.fake_resident_backend.awaiting_done_values == [True],
            timeout=1.0,
            on_tick=lambda: determinism.advance(0.01),
            description="resident awaiting-done state",
        )
        await assert_still_pending(completion_task)

        await determinism.sleep(0.18)

        result = spawn_api.spawn_rearm_sync(
            SpawnSignalInput(spawn_id=str(spawn_id)),
            ctx=RuntimeContext(spawn_id=spawn_id),
            prepared=prepared,
        )
        assert result.status == "succeeded"

        await determinism.sleep(0.05)
        await determinism.sleep(0.03)
        await assert_still_pending(completion_task)

        await determinism.sleep(0.2)
        outcome = await completion_task
        assert outcome is not None
        assert outcome.status == "timed_out"
        assert outcome.error == "resident_deadline_expired"
        await wait_until(
            lambda: reaped_spawn_ids == ["p2"],
            description="resident descendant cleanup",
        )
        assert reaped_spawn_ids == ["p2"]
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
        outcome = await manager.wait_for_completion(spawn_id)
        assert outcome is not None
        assert outcome.status == "succeeded"
    finally:
        await manager.stop_spawn(spawn_id)

@pytest.mark.asyncio
async def test_resident_stream_close_with_dead_backend_fails_while_child_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.streaming import resident_drain as resident_drain_module

    determinism = AsyncDeterminism(start=100.0)
    monkeypatch.setattr(resident_drain_module.time, "monotonic", determinism.clock.monotonic)
    spawn_id = SpawnId("p1")
    start_row(tmp_path, str(spawn_id), HarnessId.OPENCODE, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, str(spawn_id))
    connection = FakeResidentConnection(HarnessId.OPENCODE)
    manager = await start_manager(tmp_path, connection, spawn_id=spawn_id)

    connection.emit(resident_event(HarnessId.OPENCODE, "session.idle", {}))
    completion = asyncio.create_task(manager.wait_for_completion(spawn_id))

    try:
        await wait_until(
            lambda: connection.fake_resident_backend.awaiting_done_values == [True],
            description="resident awaiting-done state",
        )
        await assert_still_pending(completion)
        connection.mark_failed()
        connection.close_stream()

        outcome = await completion
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.error == "backend_dead_while_awaiting_done"
    finally:
        await manager.stop_spawn(spawn_id)

@pytest.mark.asyncio
async def test_resident_stream_close_with_stalled_backend_is_not_dead_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.streaming import resident_drain as resident_drain_module

    determinism = AsyncDeterminism(start=100.0)
    monkeypatch.setattr(resident_drain_module.time, "monotonic", determinism.clock.monotonic)
    spawn_id = SpawnId("p1")
    start_row(tmp_path, str(spawn_id), HarnessId.OPENCODE, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, str(spawn_id))
    connection = FakeResidentConnection(HarnessId.OPENCODE)
    manager = await start_manager(tmp_path, connection, spawn_id=spawn_id)

    connection.emit(resident_event(HarnessId.OPENCODE, "session.idle", {}))
    completion = asyncio.create_task(manager.wait_for_completion(spawn_id))

    try:
        await wait_until(
            lambda: connection.fake_resident_backend.awaiting_done_values == [True],
            description="resident awaiting-done state",
        )
        await assert_still_pending(completion)
        connection.mark_stalled()
        connection.close_stream()

        outcome = await completion
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
    from meridian.lib.streaming import resident_drain as resident_drain_module

    determinism = AsyncDeterminism(start=0.0)
    determinism.install(monkeypatch, monotonic_modules=(resident_drain_module,))
    determinism.install_on_running_loop(monkeypatch)

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
        await determinism.sleep(0.03)
        assert not completion_task.done()

        await determinism.sleep(0.08)
        outcome = await completion_task
        assert outcome is not None
        assert outcome.status == "timed_out"
        assert outcome.error == "resident_deadline_expired"
        await wait_until(
            lambda: reaped_spawn_ids == ["p2"],
            description="resident descendant cleanup",
        )
        assert reaped_spawn_ids == ["p2"]
        child = spawn_store.get_spawn(tmp_path, SpawnId("p2"))
        assert child is not None
        assert child.status == "cancelled"
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
        outcome = await manager.wait_for_completion(spawn_id)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert extract_codex_report(LocalStore(root_dir=tmp_path / "spawns"), spawn_id) == (
            "Resident report."
        )
    finally:
        await manager.stop_spawn(spawn_id)
