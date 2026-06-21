"""Integration coverage for spawn-scope task-dir CLI backends."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.ops.context import (
    TaskDirClearInput,
    TaskDirInput,
    TaskDirSetInput,
    task_dir_clear_sync,
    task_dir_set_sync,
    task_dir_sync,
)
from meridian.lib.ops.runtime import build_runtime
from meridian.lib.ops.spawn.api import SpawnAgentsInput, spawn_agents_sync
from meridian.lib.ops.work_attachment import set_session_work_attachment
from meridian.lib.ops.work_lifecycle import WorkTaskDirInput, work_task_dir_sync
from meridian.lib.state import session_store, work_store
from meridian.lib.state.spawn_scope import write_spawn_scope_task_dir

pytestmark = pytest.mark.slow


def _seed_project(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True)
    (project_root / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n', encoding="utf-8")
    (project_root / ".meridian").mkdir(parents=True)
    return project_root


def _seed_agent(project_root: Path, name: str, display_name: str) -> None:
    agents_dir = project_root / ".mars" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{name}.md").write_text(
        "\n".join(
            [
                "---",
                f"name: {display_name}",
                "model-policies:",
                "  - match: {alias: gpt55}",
                "    override: {effort: medium}",
                "---",
                "",
                "Profile body.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_task_dir_query_uses_inherited_env_when_no_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    inherited = tmp_path / "bootstrap-worktree"
    inherited.mkdir(parents=True)
    monkeypatch.chdir(project_root)
    monkeypatch.setenv("MERIDIAN_TASK_DIR", inherited.as_posix())
    build_runtime(project_root)

    output = task_dir_sync(TaskDirInput())

    assert output.task_dir == inherited.resolve().as_posix()
    assert output.source == "inherited"


def test_task_dir_set_then_query_reflects_scope_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    scope_dir = tmp_path / "scope-worktree"
    scope_dir.mkdir(parents=True)
    inherited = tmp_path / "stale-inherited"
    inherited.mkdir(parents=True)
    monkeypatch.chdir(project_root)
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p-scope")
    monkeypatch.setenv("MERIDIAN_TASK_DIR", inherited.as_posix())
    build_runtime(project_root)

    task_dir_set_sync(TaskDirSetInput(path=scope_dir.as_posix()))
    output = task_dir_sync(TaskDirInput())

    assert output.task_dir == scope_dir.resolve().as_posix()
    assert output.source == "scope"
    assert output.spawn_id == "p-scope"


def test_task_dir_clear_skips_stale_inherited_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    scope_dir = tmp_path / "scope-worktree"
    scope_dir.mkdir(parents=True)
    inherited = tmp_path / "stale-inherited"
    inherited.mkdir(parents=True)
    monkeypatch.chdir(project_root)
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p-clear")
    monkeypatch.setenv("MERIDIAN_TASK_DIR", inherited.as_posix())
    runtime = build_runtime(project_root)
    write_spawn_scope_task_dir(runtime.project_root, "p-clear", scope_dir)

    task_dir_clear_sync(TaskDirClearInput())
    output = task_dir_sync(TaskDirInput())

    assert output.task_dir == project_root.resolve().as_posix()
    assert output.source == "project-root"
    assert output.task_dir != inherited.as_posix()


def test_task_dir_set_and_clear_require_spawn_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True)
    monkeypatch.chdir(project_root)
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)
    build_runtime(project_root)

    with pytest.raises(ValueError, match="Not in a session"):
        task_dir_set_sync(TaskDirSetInput(path=task_dir.as_posix()))
    with pytest.raises(ValueError, match="Not in a session"):
        task_dir_clear_sync(TaskDirClearInput())


def test_work_task_dir_ignores_spawn_scope_and_inherited_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    runtime = build_runtime(project_root)
    work_task_dir = tmp_path / "work-task"
    scope_dir = tmp_path / "scope-task"
    inherited = tmp_path / "inherited-task"
    work_task_dir.mkdir(parents=True)
    scope_dir.mkdir(parents=True)
    inherited.mkdir(parents=True)

    item = work_store.create_work_item(runtime.authority.project_state_dir, "feature", "", None)
    work_store.update_work_item_task_dir(
        runtime.authority.project_state_dir,
        item.name,
        task_dir=work_task_dir.as_posix(),
    )
    session_store.start_session(
        runtime.authority.runtime_root,
        harness="codex",
        harness_session_id="session-work",
        model="gpt-5.4",
        chat_id="chat-work",
    )
    set_session_work_attachment(
        runtime.authority.runtime_root,
        chat_id="chat-work",
        work_id=item.name,
    )

    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p-work")
    monkeypatch.setenv("MERIDIAN_TASK_DIR", inherited.as_posix())
    write_spawn_scope_task_dir(runtime.project_root, "p-work", scope_dir)

    output = work_task_dir_sync(
        WorkTaskDirInput(
            chat_id="chat-work",
            project_root=project_root.as_posix(),
        )
    )

    assert output.task_dir == work_task_dir.resolve().as_posix()


def test_spawn_agents_lists_seeded_profile_names(tmp_path: Path) -> None:
    project_root = _seed_project(tmp_path)
    _seed_agent(project_root, "coder", "Coder")
    _seed_agent(project_root, "reviewer", "Reviewer")

    output = spawn_agents_sync(SpawnAgentsInput(project_root=project_root.as_posix()))

    assert output.names == ("Coder", "Reviewer")
    assert output.format_text(None) == "Coder\nReviewer"
