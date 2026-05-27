from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.ops.runtime import resolve_roots
from meridian.lib.ops.work_attachment import set_session_work_attachment
from meridian.lib.ops.work_lifecycle import (
    WorkDoneInput,
    WorkRenameInput,
    WorkStartInput,
    WorkTaskDirInput,
    work_done_sync,
    work_rename_sync,
    work_start_sync,
    work_task_dir_sync,
)
from meridian.lib.state import session_store, work_store


def _setup_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    roots = resolve_roots(project_root.as_posix())
    return project_root, roots.project_state_dir, roots.runtime_root


def test_work_start_task_dir_sets_metadata(tmp_path: Path) -> None:
    project_root, project_state_dir, _runtime_root = _setup_project(tmp_path)
    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True, exist_ok=True)

    output = work_start_sync(
        WorkStartInput(
            label="feature-x",
            task_dir=task_dir.as_posix(),
            project_root=project_root.as_posix(),
        )
    )

    assert output.name == "feature-x"
    assert output.task_dir == task_dir.resolve().as_posix()
    item = work_store.get_work_item(project_state_dir, "feature-x")
    assert item is not None
    assert item.task_dir == task_dir.resolve().as_posix()


def test_work_start_invalid_task_dir_fails_without_creating_work_item(tmp_path: Path) -> None:
    project_root, project_state_dir, _runtime_root = _setup_project(tmp_path)

    with pytest.raises(ValueError, match="task_dir does not exist"):
        work_start_sync(
            WorkStartInput(
                label="feature-invalid",
                task_dir=(tmp_path / "missing-task-dir").as_posix(),
                project_root=project_root.as_posix(),
            )
        )

    assert work_store.get_work_item(project_state_dir, "feature-invalid") is None


def test_work_task_dir_print_without_active_work_returns_project_root(tmp_path: Path) -> None:
    project_root, _project_state_dir, _runtime_root = _setup_project(tmp_path)

    output = work_task_dir_sync(
        WorkTaskDirInput(
            chat_id="chat-1",
            project_root=project_root.as_posix(),
        )
    )

    assert output.task_dir == project_root.resolve().as_posix()


def test_work_task_dir_set_and_clear_updates_active_item(tmp_path: Path) -> None:
    project_root, project_state_dir, runtime_root = _setup_project(tmp_path)
    item = work_store.create_work_item(project_state_dir, "feature-y", "", None)
    session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="session-chat-2",
        model="gpt-5.4",
        chat_id="chat-2",
    )
    set_session_work_attachment(runtime_root, chat_id="chat-2", work_id=item.name)

    task_dir = tmp_path / "feature-y-task"
    task_dir.mkdir(parents=True, exist_ok=True)

    set_output = work_task_dir_sync(
        WorkTaskDirInput(
            task_dir=task_dir.as_posix(),
            chat_id="chat-2",
            project_root=project_root.as_posix(),
        )
    )
    assert set_output.task_dir == task_dir.resolve().as_posix()

    stored = work_store.get_work_item(project_state_dir, item.name)
    assert stored is not None
    assert stored.task_dir == task_dir.resolve().as_posix()

    clear_output = work_task_dir_sync(
        WorkTaskDirInput(
            clear=True,
            chat_id="chat-2",
            project_root=project_root.as_posix(),
        )
    )
    assert clear_output.task_dir == project_root.resolve().as_posix()

    cleared = work_store.get_work_item(project_state_dir, item.name)
    assert cleared is not None
    assert cleared.task_dir is None


def test_work_task_dir_set_requires_active_work(tmp_path: Path) -> None:
    project_root, _project_state_dir, _runtime_root = _setup_project(tmp_path)
    task_dir = tmp_path / "missing-active"
    task_dir.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="No active work item"):
        work_task_dir_sync(
            WorkTaskDirInput(
                task_dir=task_dir.as_posix(),
                chat_id="chat-no-work",
                project_root=project_root.as_posix(),
            )
        )


def test_done_and_rename_do_not_mutate_task_dir_filesystem(tmp_path: Path) -> None:
    project_root, project_state_dir, _runtime_root = _setup_project(tmp_path)
    task_dir = tmp_path / "external-dir"
    task_dir.mkdir(parents=True, exist_ok=True)

    work = work_store.create_work_item(project_state_dir, "rename-me", "", None)
    work_store.update_work_item_task_dir(project_state_dir, work.name, task_dir=task_dir.as_posix())

    renamed = work_rename_sync(
        WorkRenameInput(
            work_id=work.name,
            new_name="renamed-work",
            project_root=project_root.as_posix(),
        )
    )
    assert renamed.changed is True
    assert task_dir.is_dir()

    done_output = work_done_sync(
        WorkDoneInput(
            work_id="renamed-work",
            project_root=project_root.as_posix(),
        )
    )
    assert done_output.status == "done"
    assert task_dir.is_dir()
