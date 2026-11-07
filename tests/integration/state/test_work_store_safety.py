from __future__ import annotations

import json
import multiprocessing
import time
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

import meridian.lib.state.work_store as work_store
from meridian.lib.core.logging import warning_cache_scope
from meridian.lib.state import work_repository
from meridian.lib.state.work_state import WorkItem
from tests.conftest import posix_only


def _update_work_field(
    runtime_root: Path,
    field: str,
) -> None:
    original_read = work_repository.load_work_item_from_dir

    def delayed_read(work_dir: Path, **kwargs: Any) -> WorkItem:
        item = original_read(work_dir, **kwargs)
        time.sleep(0.1)
        return item

    work_repository.load_work_item_from_dir = delayed_read
    if field == "description":
        work_repository.update_work_item(runtime_root, "shared-task", description="new description")
    else:
        work_repository.update_work_item_task_dir(
            runtime_root, "shared-task", task_dir="/new/task-dir"
        )


def _state_root(tmp_path: Path) -> Path:
    # Use a non-.meridian name so _project_paths_for_work_store treats this as a
    # synthetic test root and returns ProjectPaths.from_root_dir() directly,
    # bypassing context resolution.
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


@posix_only
def test_concurrent_field_updates_do_not_lose_each_other(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    work_repository.create_work_item(runtime_root, "shared-task")
    context = multiprocessing.get_context("fork")
    processes = [
        context.Process(target=_update_work_field, args=(runtime_root, field))
        for field in ("description", "task_dir")
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)

    assert [process.exitcode for process in processes] == [0, 0]
    item = work_store.get_work_item(runtime_root, "shared-task")
    assert item is not None
    assert item.description == "new description"
    assert item.task_dir == "/new/task-dir"


def test_work_status_preserves_unknown_fields_through_mutation(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    item = work_repository.create_work_item(
        runtime_root,
        "future-field",
        description="keep this description",
        goal="keep this goal",
    )
    status_path = runtime_root / "work" / item.name / "__status.json"
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    payload["future_field"] = {"from": "newer build"}
    payload["worktree"]["future_worktree_field"] = ["also", "newer"]
    status_path.write_text(json.dumps(payload), encoding="utf-8")

    with capture_logs() as logs, warning_cache_scope():
        assert work_store.get_work_item(runtime_root, item.name) is not None
        loaded = work_repository.update_work_item(
            runtime_root,
            item.name,
            description="changed description",
        )

    assert loaded.description == "changed description"
    assert loaded.goal == "keep this goal"
    rewritten = json.loads(status_path.read_text(encoding="utf-8"))
    assert rewritten["future_field"] == {"from": "newer build"}
    assert rewritten["worktree"]["future_worktree_field"] == ["also", "newer"]
    unknown_warnings = [
        log for log in logs if log["event"] == "Keeping unknown work item metadata fields."
    ]
    assert len(unknown_warnings) == 1
    assert unknown_warnings[0]["path"] == str(status_path)

    with capture_logs() as next_command_logs, warning_cache_scope():
        assert work_store.get_work_item(runtime_root, item.name) is not None
    assert (
        sum(
            log["event"] == "Keeping unknown work item metadata fields."
            for log in next_command_logs
        )
        == 1
    )


def test_newer_work_status_is_visible_and_refused_for_mutation(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    item = work_repository.create_work_item(
        runtime_root,
        "newer-status",
        description="newer description",
    )
    status_path = runtime_root / "work" / item.name / "__status.json"
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    payload["v"] = 2
    status_path.write_text(json.dumps(payload), encoding="utf-8")
    original = status_path.read_bytes()

    loaded = work_store.get_work_item(runtime_root, item.name)

    assert loaded is not None
    assert loaded.status == "open"
    assert loaded.display_status == "requires-newer-meridian"
    assert "Upgrade Meridian" in loaded.description
    with pytest.raises(ValueError, match="Upgrade Meridian"):
        work_repository.update_work_item(runtime_root, item.name, status="blocked")
    assert status_path.read_bytes() == original


def test_namespace_mutations_validate_every_located_copy_before_disk_changes(
    tmp_path: Path,
) -> None:
    runtime_root = _state_root(tmp_path)
    item = work_repository.create_work_item(runtime_root, "version-gate")
    active_dir = runtime_root / "work" / item.name
    archived_dir = runtime_root / "archive" / "work" / item.name
    archived_dir.mkdir(parents=True)
    archived_payload = json.loads((active_dir / "__status.json").read_text(encoding="utf-8"))
    archived_payload["v"] = 2
    (archived_dir / "__status.json").write_text(json.dumps(archived_payload), encoding="utf-8")
    (archived_dir / "notes.md").write_text("keep me\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Upgrade Meridian"):
        work_repository.archive_work_item(runtime_root, item.name)

    assert active_dir.is_dir()
    assert (archived_dir / "notes.md").read_text(encoding="utf-8") == "keep me\n"

    archived_dir.rename(runtime_root / "archive-copy")
    active_payload = json.loads((active_dir / "__status.json").read_text(encoding="utf-8"))
    active_payload["v"] = 2
    (active_dir / "__status.json").write_text(json.dumps(active_payload), encoding="utf-8")

    with pytest.raises(ValueError, match="Upgrade Meridian"):
        work_repository.rename_work_item(runtime_root, item.name, "renamed")

    assert active_dir.is_dir()
    assert not (runtime_root / "work" / "renamed").exists()


def test_interrupted_archive_leaves_the_item_done_where_it_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between the move and the status write loses nothing and reads as done."""

    runtime_root = _state_root(tmp_path)
    item = work_repository.create_work_item(runtime_root, "archive-crash")
    work_repository.update_work_item(runtime_root, item.name, status="blocked")
    (runtime_root / "work" / item.name / "notes.md").write_text("keep\n", encoding="utf-8")

    def crash_before_metadata_write(_work_dir: Path, _item: WorkItem) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(work_repository, "_write_item", crash_before_metadata_write)
    with pytest.raises(OSError, match="simulated crash"):
        work_repository.archive_work_item(runtime_root, item.name)

    archived = runtime_root / "archive" / "work" / item.name
    assert (archived / "notes.md").read_text(encoding="utf-8") == "keep\n"
    assert not (runtime_root / "work" / item.name).exists()
    loaded = work_store.get_work_item(runtime_root, item.name)
    assert loaded is not None
    assert loaded.status == "done"


def test_interrupted_reopen_leaves_the_item_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash before the move leaves the item archived, files intact, reopen-able."""

    runtime_root = _state_root(tmp_path)
    item = work_repository.create_work_item(runtime_root, "reopen-crash")
    (runtime_root / "work" / item.name / "notes.md").write_text("keep\n", encoding="utf-8")
    work_repository.archive_work_item(runtime_root, item.name)

    def crash_before_metadata_write(_work_dir: Path, _item: WorkItem) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(work_repository, "_write_item", crash_before_metadata_write)
    with pytest.raises(OSError, match="simulated crash"):
        work_repository.reopen_work_item(runtime_root, item.name)

    archived = runtime_root / "archive" / "work" / item.name
    assert (archived / "notes.md").is_file()
    assert not (runtime_root / "work" / item.name).exists()
    loaded = work_store.get_work_item(runtime_root, item.name)
    assert loaded is not None
    assert loaded.status == "done"

    # The interrupted reopen is repaired by running it again, not by a sweep.
    monkeypatch.undo()
    reopened = work_repository.reopen_work_item(runtime_root, item.name)
    assert reopened.status == "open"
    assert (runtime_root / "work" / item.name / "notes.md").is_file()
