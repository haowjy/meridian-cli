from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.ops.context import ContextInput, WorkCurrentInput, context_sync, work_current_sync
from meridian.lib.ops.work_lifecycle import (
    WorkClearInput,
    WorkDoneInput,
    WorkStartInput,
    WorkUpdateInput,
    work_clear_sync,
    work_done_sync,
    work_start_sync,
    work_update_sync,
)
from meridian.lib.state.current_work import get_current_work_id

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def _setup_project_runtime(tmp_path: Path, monkeypatch: MonkeyPatch) -> tuple[Path, Path]:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    runtime_root = project_root / ".meridian"
    user_config = tmp_path / "user-config.toml"
    user_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_CONFIG", user_config.as_posix())
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", runtime_root.as_posix())
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_DIR", raising=False)
    monkeypatch.chdir(project_root)
    return project_root, runtime_root


def test_context_work_and_work_current_use_persisted_active_work_when_env_unset(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root, runtime_root = _setup_project_runtime(tmp_path, monkeypatch)

    started = work_start_sync(
        WorkStartInput(
            label="active-work-resolution",
            description="persist active work for context/work current",
            chat_id="",
            project_root=project_root.as_posix(),
        )
    )

    expected_work_dir = project_root / ".meridian" / "work" / started.name
    assert get_current_work_id(runtime_root) == started.name
    assert work_current_sync(WorkCurrentInput()).work_dir == expected_work_dir.as_posix()
    assert context_sync(ContextInput()).resolve_name("work") == expected_work_dir.as_posix()

    work_clear_sync(WorkClearInput(chat_id="", project_root=project_root.as_posix()))

    assert get_current_work_id(runtime_root) is None
    assert work_current_sync(WorkCurrentInput()).work_dir is None
    assert context_sync(ContextInput()).resolve_name("work") == ""


def test_work_done_clears_persisted_current_work_pointer(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root, runtime_root = _setup_project_runtime(tmp_path, monkeypatch)
    started = work_start_sync(
        WorkStartInput(
            label="done-clears-current-work",
            description="regression",
            chat_id="",
            project_root=project_root.as_posix(),
        )
    )

    assert get_current_work_id(runtime_root) == started.name

    work_done_sync(WorkDoneInput(work_id=started.name, project_root=project_root.as_posix()))

    assert get_current_work_id(runtime_root) is None
    assert work_current_sync(WorkCurrentInput()).work_dir is None
    assert context_sync(ContextInput()).resolve_name("work") == ""


def test_work_update_done_clears_persisted_current_work_pointer(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root, runtime_root = _setup_project_runtime(tmp_path, monkeypatch)
    started = work_start_sync(
        WorkStartInput(
            label="update-done-clears-current-work",
            description="regression",
            chat_id="",
            project_root=project_root.as_posix(),
        )
    )

    assert get_current_work_id(runtime_root) == started.name

    work_update_sync(
        WorkUpdateInput(
            work_id=started.name,
            status="done",
            project_root=project_root.as_posix(),
        )
    )

    assert get_current_work_id(runtime_root) is None
    assert work_current_sync(WorkCurrentInput()).work_dir is None
    assert context_sync(ContextInput()).resolve_name("work") == ""
