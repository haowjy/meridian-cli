from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.lifecycle import SpawnLifecycleService
from meridian.lib.core.spawn_lifecycle import (
    ExecutionTerminalFacts,
    has_durable_report_completion,
)
from meridian.lib.core.spawn_service import (
    CancelOutcome,
    PrepareSpawnRequest,
    SpawnApplicationService,
)
from meridian.lib.core.types import SpawnId
from meridian.lib.launch.request import LaunchRuntime, SpawnRequest
from meridian.lib.state import spawn_store


def _runtime_root(tmp_path: Path) -> Path:
    runtime_root = tmp_path / ".meridian"
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


def _runtime_request(tmp_path: Path, runtime_root: Path) -> LaunchRuntime:
    return LaunchRuntime(
        runtime_root=runtime_root.as_posix(),
        config_root=tmp_path.as_posix(),
        control_root=tmp_path.as_posix(),
        requested_task_cwd=tmp_path.as_posix(),
        project_paths_project_root=tmp_path.as_posix(),
        project_paths_execution_cwd=tmp_path.as_posix(),
    )


def _service(runtime_root: Path) -> SpawnApplicationService:
    return SpawnApplicationService(runtime_root, SpawnLifecycleService(runtime_root))


def _start_spawn(
    runtime_root: Path,
    *,
    status: SpawnStatus = "running",
    kind: str = "child",
) -> str:
    return str(
        spawn_store.start_spawn(
            runtime_root,
            spawn_id="p1",
            chat_id="c1",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            kind=kind,
            prompt="hello",
            status=status,
        )
    )


def _start_spawn_record(
    runtime_root: Path,
    spawn_id: str,
    *,
    parent_id: str | None = None,
    status: SpawnStatus = "running",
) -> str:
    return str(
        spawn_store.start_spawn(
            runtime_root,
            spawn_id=spawn_id,
            chat_id=spawn_id,
            parent_id=parent_id,
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt=f"prompt {spawn_id}",
            status=status,
        )
    )


def _fake_launch_context_builder(child_cwd: Path) -> Any:
    def _bind_spawn_launch_context(**kwargs: object) -> SimpleNamespace:
        prepared = cast("Any", kwargs["prepared"])
        bindings = cast("Any", kwargs["bindings"])
        request = cast("SpawnRequest", prepared.request)
        spawn_id = str(bindings.spawn_id)
        runtime = cast("LaunchRuntime", kwargs["runtime"])
        return SimpleNamespace(
            resolved_request=request,
            project_root=child_cwd.parent,
            control_root=child_cwd.parent,
            task_cwd=child_cwd,
            runtime=runtime,
            work_id=None,
            binding=SimpleNamespace(
                child_cwd=child_cwd,
                environment=SimpleNamespace(
                    bind_env_overrides={"MERIDIAN_SPAWN_ID": spawn_id},
                ),
                run_params=SimpleNamespace(appended_system_prompt="", interactive=False),
            ),
        )

    return _bind_spawn_launch_context


def _patch_prepare_compose_bind(
    monkeypatch: pytest.MonkeyPatch,
    child_cwd: Path,
    *,
    reserved_spawn_id: str = "p1",
) -> None:
    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.compose_spawn_launch_surface",
        lambda **kwargs: SimpleNamespace(request=kwargs["request"]),
    )
    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.bind_spawn_launch_context",
        _fake_launch_context_builder(child_cwd),
    )
    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.spawn_store.reserve_spawn_id",
        lambda _root: reserved_spawn_id,
    )


@pytest.mark.asyncio
async def test_prepare_persists_trimmed_goal_from_spawn_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    child_cwd = tmp_path / "child-cwd"
    child_cwd.mkdir()
    _patch_prepare_compose_bind(monkeypatch, child_cwd, reserved_spawn_id="p1")

    service = _service(runtime_root)
    prepared = await service.prepare(
        PrepareSpawnRequest(
            request=SpawnRequest(
                prompt="run it",
                model="gpt-5.4",
                harness="codex",
                agent="coder",
                goal="  keep scope tight  ",
            ),
            runtime=_runtime_request(tmp_path, runtime_root),
            harness_registry=cast("Any", SimpleNamespace()),
            chat_id="c1",
        )
    )

    row = spawn_store.get_spawn(runtime_root, prepared.spawn_id)
    assert row is not None
    assert row.goal == "keep scope tight"
    assert prepared.connection_config.control_root == tmp_path
    assert prepared.connection_config.task_cwd == child_cwd
    assert prepared.connection_config.pi_child_wave_timeout_seconds == 300.0


@pytest.mark.asyncio
async def test_prepare_spawn_rejects_blank_goal_without_persisting_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    child_cwd = tmp_path / "child-cwd"
    child_cwd.mkdir()
    _patch_prepare_compose_bind(monkeypatch, child_cwd)

    service = _service(runtime_root)

    with pytest.raises(ValueError, match="--goal cannot be empty"):
        await service.prepare_spawn(
            request=SpawnRequest(
                prompt="run it",
                model="gpt-5.4",
                harness="codex",
                agent="coder",
                goal="   ",
            ),
            runtime=_runtime_request(tmp_path, runtime_root),
            harness_registry=cast("Any", SimpleNamespace()),
            chat_id="c1",
        )

    assert spawn_store.list_spawns(runtime_root) == []


@pytest.mark.asyncio
async def test_complete_spawn_persists_terminal_intent_before_mark_finalizing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    spawn_id = _start_spawn(runtime_root, status="running")
    service = _service(runtime_root)
    original_mark_finalizing = service.lifecycle.mark_finalizing

    def _assert_intent_then_mark(target_spawn_id: str) -> bool:
        row = spawn_store.get_spawn(runtime_root, target_spawn_id)
        assert row is not None
        assert row.runner_exit_status == "failed"
        assert row.runner_exit_code == 9
        assert row.runner_exit_error == "launch_failed"
        assert row.runner_exit_at is not None
        return original_mark_finalizing(target_spawn_id)

    monkeypatch.setattr(service.lifecycle, "mark_finalizing", _assert_intent_then_mark)

    outcome = await service.complete_spawn(
        SpawnId(spawn_id),
        "failed",
        9,
        origin="launch_failure",
        error="launch_failed",
    )

    assert outcome.wrote is True
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "failed"
    assert row.runner_exit_status == "failed"
    assert row.runner_exit_code == 9
    assert row.runner_exit_error == "launch_failed"


@pytest.mark.asyncio
async def test_complete_execution_does_not_backfill_runner_exit_on_terminal_row(
    tmp_path: Path,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    spawn_id = _start_spawn(runtime_root, status="running")
    spawn_store.finalize_spawn(
        runtime_root,
        spawn_id,
        "failed",
        1,
        origin="launch_failure",
        error="bootstrap_failed",
        finished_at="2026-04-12T14:05:00Z",
    )
    service = _service(runtime_root)

    outcome = await service.complete_execution(
        SpawnId(spawn_id),
        ExecutionTerminalFacts(exit_code=0),
        origin="runner",
    )

    assert outcome.completion.snapshot is not None
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.runner_exit_status is None
    assert row.runner_exit_code is None


@pytest.mark.asyncio
async def test_complete_execution_prefers_cancel_intent_until_durable_completion(
    tmp_path: Path,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    spawn_id = _start_spawn(runtime_root, status="running")
    spawn_store.record_cancel_intent(
        runtime_root,
        spawn_id,
        exit_code=130,
        error="cancelled",
        requested_at="2026-06-03T01:00:00Z",
    )
    service = _service(runtime_root)

    outcome = await service.complete_execution(
        SpawnId(spawn_id),
        ExecutionTerminalFacts(exit_code=0, durable_report_completion=False),
        origin="runner",
    )

    assert outcome.resolved.status == "cancelled"
    assert outcome.resolved.exit_code == 130
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "cancelled"


@pytest.mark.asyncio
async def test_complete_execution_keeps_cancel_for_synthetic_failure_report(
    tmp_path: Path,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    spawn_id = _start_spawn(runtime_root, status="running")
    spawn_store.record_cancel_intent(
        runtime_root,
        spawn_id,
        exit_code=130,
        error="cancelled",
        requested_at="2026-06-03T01:00:00Z",
    )
    service = _service(runtime_root)
    synthetic_report = "# Spawn failed\n\nCursor subprocess exited with code 130.\n"

    outcome = await service.complete_execution(
        SpawnId(spawn_id),
        ExecutionTerminalFacts(
            exit_code=130,
            failure_reason="cancelled",
            cancellation_observed=True,
            durable_report_completion=has_durable_report_completion(synthetic_report),
        ),
        origin="runner",
    )

    assert synthetic_report.startswith("# Spawn failed")
    assert outcome.resolved.status == "cancelled"
    assert outcome.resolved.exit_code == 130
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "cancelled"
    assert row.exit_code == 130


@pytest.mark.asyncio
async def test_complete_execution_keeps_cancel_for_codex_close_error_report(
    tmp_path: Path,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    spawn_id = _start_spawn(runtime_root, status="running")
    spawn_store.record_cancel_intent(
        runtime_root,
        spawn_id,
        exit_code=130,
        error="cancelled",
        requested_at="2026-06-03T01:00:00Z",
    )
    service = _service(runtime_root)
    close_error_report = (
        '# Report\n\n{"type":"error","message":"no close frame received or sent"}\n'
    )

    outcome = await service.complete_execution(
        SpawnId(spawn_id),
        ExecutionTerminalFacts(
            exit_code=0,
            durable_report_completion=has_durable_report_completion(close_error_report),
        ),
        origin="runner",
    )

    assert outcome.resolved.status == "cancelled"
    assert outcome.resolved.exit_code == 130
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "cancelled"
    assert row.exit_code == 130


@pytest.mark.asyncio
async def test_complete_execution_keeps_cancel_for_claude_aborted_streaming_result(
    tmp_path: Path,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    spawn_id = _start_spawn(runtime_root, status="running")
    spawn_store.record_cancel_intent(
        runtime_root,
        spawn_id,
        exit_code=130,
        error="cancelled",
        requested_at="2026-06-03T01:00:00Z",
    )
    service = _service(runtime_root)
    aborted_result = (
        '# Report\n\n{"type":"result","is_error":true,'
        '"terminal_reason":"aborted_streaming","result":""}\n'
    )

    outcome = await service.complete_execution(
        SpawnId(spawn_id),
        ExecutionTerminalFacts(
            exit_code=0,
            durable_report_completion=has_durable_report_completion(aborted_result),
        ),
        origin="runner",
    )

    assert outcome.resolved.status == "cancelled"
    assert outcome.resolved.exit_code == 130
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "cancelled"
    assert row.exit_code == 130


@pytest.mark.asyncio
async def test_complete_execution_durable_completion_wins_over_cancel_intent(
    tmp_path: Path,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    spawn_id = _start_spawn(runtime_root, status="running")
    spawn_store.record_cancel_intent(
        runtime_root,
        spawn_id,
        exit_code=130,
        error="cancelled",
        requested_at="2026-06-03T01:00:00Z",
    )
    service = _service(runtime_root)
    completion_report = '# Report\n\n{"message":"Done."}\n'

    outcome = await service.complete_execution(
        SpawnId(spawn_id),
        ExecutionTerminalFacts(
            exit_code=130,
            durable_report_completion=has_durable_report_completion(completion_report),
        ),
        origin="runner",
    )

    assert outcome.resolved.status == "succeeded"
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "succeeded"


@pytest.mark.asyncio
async def test_complete_execution_treats_claude_success_result_as_durable_completion(
    tmp_path: Path,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    spawn_id = _start_spawn(runtime_root, status="running")
    spawn_store.record_cancel_intent(
        runtime_root,
        spawn_id,
        exit_code=130,
        error="cancelled",
        requested_at="2026-06-03T01:00:00Z",
    )
    service = _service(runtime_root)
    success_result = (
        '# Report\n\n{"type":"result","is_error":false,'
        '"terminal_reason":"end_turn","result":"OK"}\n'
    )

    outcome = await service.complete_execution(
        SpawnId(spawn_id),
        ExecutionTerminalFacts(
            exit_code=130,
            durable_report_completion=has_durable_report_completion(success_result),
        ),
        origin="runner",
    )

    assert outcome.resolved.status == "succeeded"
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "succeeded"


@pytest.mark.asyncio
async def test_cancel_public_surface_backfills_cancel_intent_for_managed_primary_finalizing_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.state.primary_meta import PrimaryMetadata, write_primary_metadata

    runtime_root = _runtime_root(tmp_path)
    spawn_id = _start_spawn(runtime_root, status="finalizing", kind="primary")
    spawn_dir = runtime_root / "spawns" / spawn_id
    write_primary_metadata(
        spawn_dir,
        PrimaryMetadata(
            managed_backend=True,
            launcher_pid=7771,
            activity="finalizing",
        ),
    )
    service = _service(runtime_root)

    # Force the recorded launcher PID to read as dead regardless of the host's PID
    # assignments (7771 can be a live process on the Windows runner), so the
    # managed-primary reconciliation proceeds instead of skipping as launcher-alive.
    monkeypatch.setattr(
        "meridian.lib.state.managed_primary.is_process_alive",
        lambda *_args, **_kwargs: False,
    )

    async def _never_terminal(*_args: object, **_kwargs: object) -> Any:
        return None

    monkeypatch.setattr(service, "_wait_for_terminal", _never_terminal)

    outcome = await service.cancel(SpawnId(spawn_id))

    assert outcome.status == "cancelled"
    assert outcome.finalizing is False
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "cancelled"
    assert row.cancel_intent is not None
    assert row.cancel_intent.exit_code == 130
    assert row.cancel_intent.error == "cancelled"


@pytest.mark.asyncio
async def test_cancel_app_spawn_records_intent_without_local_force_finalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p1",
        chat_id="c1",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="hello",
        status="running",
        launch_mode="app",
    )

    class _FakeManager:
        async def stop_spawn(self, *_args: object, **_kwargs: object) -> None:
            return None

    service = SpawnApplicationService(
        runtime_root,
        SpawnLifecycleService(runtime_root),
        spawn_manager=cast("Any", _FakeManager()),
    )

    async def _never_terminal(*_args: object, **_kwargs: object) -> Any:
        return None

    monkeypatch.setattr(service, "_wait_for_terminal", _never_terminal)

    outcome = await service.cancel(SpawnId("p1"))

    assert outcome.status == "running"
    assert outcome.finalizing is True
    row = spawn_store.get_spawn(runtime_root, "p1")
    assert row is not None
    assert row.status == "running"
    assert row.cancel_intent is not None


@pytest.mark.asyncio
async def test_cancel_descendants_forces_active_subtree_to_terminal(
    tmp_path: Path,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    _start_spawn_record(runtime_root, "p1")
    _start_spawn_record(runtime_root, "p2", parent_id="p1")
    _start_spawn_record(runtime_root, "p3", parent_id="p2")
    _start_spawn_record(runtime_root, "p4", parent_id="p1")
    spawn_store.finalize_spawn(
        runtime_root,
        "p4",
        "succeeded",
        0,
        origin="runner",
        error=None,
    )
    service = _service(runtime_root)

    reaped_ids = await service.cancel_descendants(SpawnId("p1"))

    child = spawn_store.get_spawn(runtime_root, "p2")
    grandchild = spawn_store.get_spawn(runtime_root, "p3")
    already_terminal = spawn_store.get_spawn(runtime_root, "p4")
    assert child is not None
    assert grandchild is not None
    assert already_terminal is not None
    assert child.status == "cancelled"
    assert grandchild.status == "cancelled"
    assert child.cancel_intent is not None
    assert child.cancel_intent.requested_by == "system"
    assert grandchild.cancel_intent is not None
    assert grandchild.cancel_intent.requested_by == "system"
    assert already_terminal.status == "succeeded"
    assert already_terminal.cancel_intent is None
    assert reaped_ids == {"p2", "p3"}


@pytest.mark.asyncio
async def test_cancel_descendants_rescans_to_fixed_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    _start_spawn_record(runtime_root, "p1")
    _start_spawn_record(runtime_root, "p2", parent_id="p1")
    service = _service(runtime_root)
    cancelled: list[tuple[str, str]] = []

    async def _cancel(spawn_id: SpawnId, *, requested_by: str = "user") -> object:
        cancelled.append((str(spawn_id), requested_by))
        if spawn_id == SpawnId("p2") and spawn_store.get_spawn(runtime_root, "p3") is None:
            _start_spawn_record(runtime_root, "p3", parent_id="p2")
        spawn_store.finalize_spawn(
            runtime_root,
            spawn_id,
            "cancelled",
            130,
            origin="cancel",
            error="cancelled",
        )
        return CancelOutcome(
            spawn_id=str(spawn_id),
            status="cancelled",
            origin="cancel",
            exit_code=130,
        )

    monkeypatch.setattr(service, "cancel", _cancel)

    reaped_ids = await service.cancel_descendants(SpawnId("p1"))

    assert cancelled == [("p2", "system"), ("p3", "system")]
    assert reaped_ids == {"p2", "p3"}
    grandchild = spawn_store.get_spawn(runtime_root, "p3")
    assert grandchild is not None
    assert grandchild.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_descendants_reports_only_proven_terminal_reaped_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    _start_spawn_record(runtime_root, "p1")
    _start_spawn_record(runtime_root, "p2", parent_id="p1")
    _start_spawn_record(runtime_root, "p3", parent_id="p1")
    service = _service(runtime_root)

    async def _cancel(spawn_id: SpawnId, *, requested_by: str = "user") -> CancelOutcome:
        _ = requested_by
        if spawn_id == SpawnId("p2"):
            record = spawn_store.get_spawn(runtime_root, spawn_id)
            assert record is not None
            spawn_store.mark_finalizing(runtime_root, spawn_id)
            return CancelOutcome(
                spawn_id=str(spawn_id),
                status="finalizing",
                origin="cancel",
                exit_code=130,
                finalizing=True,
            )
        spawn_store.finalize_spawn(
            runtime_root,
            spawn_id,
            "cancelled",
            130,
            origin="cancel",
            error="cancelled",
        )
        return CancelOutcome(
            spawn_id=str(spawn_id),
            status="cancelled",
            origin="cancel",
            exit_code=130,
        )

    monkeypatch.setattr(service, "cancel", _cancel)

    reaped_ids = await service.cancel_descendants(SpawnId("p1"))

    assert "p2" not in reaped_ids
    assert "p3" in reaped_ids


@pytest.mark.asyncio
async def test_prepare_sets_pi_notification_timeout_from_wait_timeout_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    child_cwd = tmp_path / "child-cwd"
    child_cwd.mkdir()
    _patch_prepare_compose_bind(monkeypatch, child_cwd)

    runtime = _runtime_request(tmp_path, runtime_root).model_copy(
        update={
            "config_snapshot": {
                "wait_timeout_minutes": 30.0,
                "pi_child_wave_timeout_seconds": 45.0,
            }
        }
    )
    service = _service(runtime_root)
    prepared = await service.prepare(
        PrepareSpawnRequest(
            request=SpawnRequest(
                prompt="run it",
                model="pi-fast",
                harness="pi",
            ),
            runtime=runtime,
            harness_registry=cast("Any", SimpleNamespace()),
            chat_id="c1",
        )
    )

    assert prepared.connection_config.timeout_seconds is None
    assert prepared.connection_config.pi_notification_timeout_seconds == 1800.0
    assert prepared.connection_config.pi_child_wave_timeout_seconds == 45.0
