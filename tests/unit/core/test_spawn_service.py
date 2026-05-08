from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from meridian.lib.core.lifecycle import SpawnLifecycleService
from meridian.lib.core.spawn_service import PrepareSpawnRequest, SpawnApplicationService
from meridian.lib.launch.request import LaunchRuntime, SpawnRequest
from meridian.lib.state import spawn_store


def _runtime_root(tmp_path: Path) -> Path:
    runtime_root = tmp_path / ".meridian"
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


def _runtime_request(tmp_path: Path, runtime_root: Path) -> LaunchRuntime:
    return LaunchRuntime(
        runtime_root=runtime_root.as_posix(),
        project_paths_project_root=tmp_path.as_posix(),
        project_paths_execution_cwd=tmp_path.as_posix(),
    )


def _service(runtime_root: Path) -> SpawnApplicationService:
    return SpawnApplicationService(runtime_root, SpawnLifecycleService(runtime_root))


def _fake_launch_context_builder(child_cwd: Path) -> Any:
    def _build_launch_context(**kwargs: object) -> SimpleNamespace:
        request = cast("SpawnRequest", kwargs["request"])
        spawn_id = str(kwargs["spawn_id"])
        return SimpleNamespace(
            resolved_request=request,
            child_cwd=child_cwd,
            work_id=None,
            env_overrides={"MERIDIAN_SPAWN_ID": spawn_id},
            run_params=SimpleNamespace(appended_system_prompt=""),
        )

    return _build_launch_context


@pytest.mark.asyncio
async def test_prepare_persists_trimmed_goal_from_spawn_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    child_cwd = tmp_path / "child-cwd"
    child_cwd.mkdir()
    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.build_launch_context",
        _fake_launch_context_builder(child_cwd),
    )

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


@pytest.mark.asyncio
async def test_prepare_spawn_rejects_blank_goal_without_persisting_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    child_cwd = tmp_path / "child-cwd"
    child_cwd.mkdir()
    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.build_launch_context",
        _fake_launch_context_builder(child_cwd),
    )

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
