"""REST spawn prepare: compose once, bind-only after reserve_spawn_id."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.core.lifecycle import SpawnLifecycleService
from meridian.lib.core.spawn_lifecycle import SpawnReservation
from meridian.lib.core.spawn_service import PrepareSpawnRequest, SpawnApplicationService
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.plan import build_spawn_mars_runtime
from meridian.lib.launch.request import LaunchArgvIntent, SpawnRequest
from meridian.lib.ops.runtime import build_runtime
from tests.support.launch import stub_bundle_request_and_resolve


def _seed_project(tmp_path: Path) -> Path:
    (tmp_path / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n', encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_prepare_spawn_calls_mars_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    captured = stub_bundle_request_and_resolve(
        monkeypatch,
        model="openai-codex/gpt-5.4-mini",
        harness=HarnessId.PI,
        harness_model="openai-codex/gpt-5.4-mini",
    )
    runtime_bundle = build_runtime(project_root)
    runtime_root = project_root / ".meridian"
    runtime_root.mkdir(parents=True, exist_ok=True)
    launch_runtime = build_spawn_mars_runtime(
        runtime=runtime_bundle,
        runtime_root=runtime_root,
        control_root=project_root,
        execution_cwd=project_root.as_posix(),
        argv_intent=LaunchArgvIntent.SPEC_ONLY,
    )

    service = SpawnApplicationService(runtime_root, SpawnLifecycleService(runtime_root))
    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.spawn_store.reserve_spawn_id",
        lambda _root: "p99",
    )
    monkeypatch.setattr(
        service._lifecycle,
        "start",
        lambda _reservation, **_kwargs: "p99",
    )

    prepared = await service.prepare(
        PrepareSpawnRequest(
            request=SpawnRequest(prompt="hi", harness="pi", model="openai-codex/gpt-5.4-mini"),
            runtime=launch_runtime,
            harness_registry=get_default_harness_registry(),
        )
    )

    assert len(captured) == 1
    assert str(prepared.spawn_id) == "p99"
    assert prepared.launch_context.binding.spec.model == "openai-codex/gpt-5.4-mini"


@pytest.mark.asyncio
async def test_prepare_spawn_connection_config_keeps_control_root_when_task_dir_differs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    task_dir = tmp_path / "task-dir"
    task_dir.mkdir()
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="openai-codex/gpt-5.4-mini",
        harness=HarnessId.PI,
        harness_model="openai-codex/gpt-5.4-mini",
    )
    runtime_bundle = build_runtime(project_root)
    runtime_root = project_root / ".meridian"
    runtime_root.mkdir(parents=True, exist_ok=True)
    launch_runtime = build_spawn_mars_runtime(
        runtime=runtime_bundle,
        runtime_root=runtime_root,
        control_root=project_root,
        execution_cwd=task_dir.as_posix(),
        argv_intent=LaunchArgvIntent.SPEC_ONLY,
    )

    service = SpawnApplicationService(runtime_root, SpawnLifecycleService(runtime_root))
    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.spawn_store.reserve_spawn_id",
        lambda _root: "p100",
    )
    captured_start: dict[str, object] = {}

    def _capture_start(reservation: SpawnReservation, **_kwargs: object) -> str:
        captured_start["execution_cwd"] = reservation.execution_cwd
        captured_start["task_cwd"] = reservation.task_cwd
        return "p100"

    monkeypatch.setattr(service._lifecycle, "start", _capture_start)

    prepared = await service.prepare(
        PrepareSpawnRequest(
            request=SpawnRequest(prompt="hi", harness="pi", model="openai-codex/gpt-5.4-mini"),
            runtime=launch_runtime,
            harness_registry=get_default_harness_registry(),
        )
    )

    assert prepared.connection_config.control_root == project_root.resolve()
    assert prepared.connection_config.task_cwd is None
    assert captured_start["execution_cwd"] == project_root.resolve().as_posix()
    assert captured_start["task_cwd"] == task_dir.resolve().as_posix()
