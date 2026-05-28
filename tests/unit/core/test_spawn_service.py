from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.lifecycle import SpawnLifecycleService
from meridian.lib.core.spawn_lifecycle import ExecutionTerminalFacts
from meridian.lib.core.spawn_service import PrepareSpawnRequest, SpawnApplicationService
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

    async def _never_terminal(*_args: object, **_kwargs: object) -> Any:
        return None

    monkeypatch.setattr(service, "_wait_for_terminal", _never_terminal)

    outcome = await service.cancel(SpawnId(spawn_id))

    assert outcome.status == "finalizing"
    assert outcome.finalizing is True
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "finalizing"
    assert row.runner_exit_status == "cancelled"
    assert row.runner_exit_code == 130
    assert row.runner_exit_error == "cancel_timeout"
    assert row.runner_exit_at is not None


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
