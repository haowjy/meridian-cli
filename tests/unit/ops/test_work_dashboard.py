from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.ops.runtime import resolve_roots
from meridian.lib.ops.work_dashboard import (
    WorkDashboardInput,
    WorkDashboardOutput,
    WorkShowInput,
    work_dashboard_sync,
    work_show_sync,
)
from meridian.lib.state import work_store
from meridian.lib.state.spawn.model import SpawnRecord


def _make_spawn_record(
    spawn_id: str,
    *,
    work_id: str | None = None,
    status: SpawnStatus = "running",
) -> SpawnRecord:
    return SpawnRecord(
        id=spawn_id,
        parent_id=None,
        chat_id="c1",
        owner_chat_id=None,
        status=status,
        model="test-model",
        agent="coder",
        agent_path=None,
        skills=(),
        skill_paths=(),
        harness="codex",
        kind="child",
        desc="ambient spawn",
        work_id=work_id,
        goal=None,
        harness_session_id=None,
        execution_cwd=None,
        claude_config_dir=None,
        launch_mode="background",
        worker_pid=None,
        runner_pid=None,
        runner_created_at_epoch=None,
        prompt="test",
        started_at="2026-01-01T00:00:00Z",
        last_attempt_exited_at=None,
        last_attempt_exit_code=None,
        runner_exit_code=None,
        runner_exit_status=None,
        runner_exit_error=None,
        runner_exit_at=None,
        finished_at=None,
        exit_code=None,
        duration_secs=None,
        total_cost_usd=None,
        input_tokens=None,
        output_tokens=None,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=None,
        reasoning_tokens=None,
        cost_is_estimate=False,
        error=None,
        terminal_origin=None,
    )


def _setup_project(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    roots = resolve_roots(project_root.as_posix())
    return project_root, roots.project_state_dir


def test_work_show_includes_stored_task_dir(tmp_path: Path) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item = work_store.create_work_item(project_state_dir, "feature-a", "", None)
    task_dir = tmp_path / "feature-a-task"
    task_dir.mkdir(parents=True, exist_ok=True)
    work_store.update_work_item_task_dir(project_state_dir, item.name, task_dir=task_dir.as_posix())

    output = work_show_sync(WorkShowInput(work_id=item.name, project_root=project_root.as_posix()))

    assert output.task_dir == task_dir.resolve().as_posix()
    formatted = output.format_text()
    assert f"Task dir: {task_dir.resolve().as_posix()}" in formatted
    assert "Worktree" not in formatted


def test_work_show_includes_cleared_task_dir_as_null(tmp_path: Path) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item = work_store.create_work_item(project_state_dir, "feature-b", "", None)
    task_dir = tmp_path / "feature-b-task"
    task_dir.mkdir(parents=True, exist_ok=True)
    work_store.update_work_item_task_dir(project_state_dir, item.name, task_dir=task_dir.as_posix())
    work_store.update_work_item_task_dir(project_state_dir, item.name, task_dir=None)

    output = work_show_sync(WorkShowInput(work_id=item.name, project_root=project_root.as_posix()))

    assert output.task_dir is None


def test_work_dashboard_groups_null_work_id_spawn_as_ungrouped(
    tmp_path: Path,
) -> None:
    project_root, _ = _setup_project(tmp_path)
    ambient_spawn = _make_spawn_record("p10", work_id=None)

    with patch(
        "meridian.lib.state.reaper.reconcile_spawns",
        return_value=[ambient_spawn],
    ):
        output = work_dashboard_sync(
            WorkDashboardInput(project_root=project_root.as_posix()),
        )

    assert output.items == ()
    assert len(output.ungrouped_spawns) == 1
    assert output.ungrouped_spawns[0].id == "p10"
    formatted = output.format_text()
    assert "(no work)" in formatted
    assert "p10" in formatted


def test_work_dashboard_groups_named_work_id_spawn(
    tmp_path: Path,
) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    work_store.create_work_item(project_state_dir, "feature-x", "goal", None)
    named_spawn = _make_spawn_record("p11", work_id="feature-x")

    with patch(
        "meridian.lib.state.reaper.reconcile_spawns",
        return_value=[named_spawn],
    ):
        output: WorkDashboardOutput = work_dashboard_sync(
            WorkDashboardInput(project_root=project_root.as_posix()),
        )

    assert len(output.items) == 1
    assert output.items[0].name == "feature-x"
    assert output.items[0].spawns[0].id == "p11"
    assert output.ungrouped_spawns == ()
