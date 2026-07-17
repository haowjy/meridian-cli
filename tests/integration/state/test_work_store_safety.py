from __future__ import annotations

import json
import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

import meridian.lib.state.work_store as work_store
from meridian.lib.state import work_repository
from tests.conftest import posix_only


def _update_work_field(
    runtime_root: Path,
    field: str,
) -> None:
    original_read = work_repository._work_item_from_dir

    def delayed_read(work_dir: Path, **kwargs: Any) -> work_store.WorkItem:
        item = original_read(work_dir, **kwargs)
        time.sleep(0.1)
        return item

    work_repository._work_item_from_dir = delayed_read
    if field == "description":
        work_store.update_work_item(runtime_root, "shared-task", description="new description")
    else:
        work_store.update_work_item_task_dir(runtime_root, "shared-task", task_dir="/new/task-dir")


def _state_root(tmp_path: Path) -> Path:
    # Use a non-.meridian name so _project_paths_for_work_store treats this as a
    # synthetic test root and returns ProjectPaths.from_root_dir() directly,
    # bypassing context resolution.
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _ensure_shared_task_name(_: int, *, runtime_root: Path) -> str:
    return work_store.ensure_work_item_metadata(runtime_root, "shared-task").name


def test_ensure_work_item_metadata_with_concurrent_calls(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)

    total = 24
    with ThreadPoolExecutor(max_workers=8) as pool:
        names = list(
            pool.map(partial(_ensure_shared_task_name, runtime_root=runtime_root), range(total))
        )

    assert len(names) == total
    assert set(names) == {"shared-task"}
    assert (runtime_root / "work" / "shared-task" / "__status.json").exists()


@posix_only
def test_concurrent_field_updates_do_not_lose_each_other(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    work_store.create_work_item(runtime_root, "shared-task")
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


def test_get_work_item_is_pure_and_explicit_heal_repairs_malformed_status(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    item = work_store.create_work_item(runtime_root, "repair-malformed")

    status_path = runtime_root / "work" / item.name / "__status.json"
    status_path.write_text("not json", encoding="utf-8")

    loaded = work_store.get_work_item(runtime_root, item.name)
    assert loaded is not None
    assert loaded.name == item.name
    assert loaded.status == "open"
    assert status_path.read_text(encoding="utf-8") == "not json"

    work_store.heal_work_item(runtime_root, item.name)
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["status"] == "open"
    assert payload["created_at"]
    assert payload["archived_at"] is None
def test_delete_without_force_succeeds_for_status_only_directory(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    item = work_store.create_work_item(runtime_root, "delete-me")

    deleted, had_artifacts = work_store.delete_work_item(runtime_root, item.name, force=False)

    assert deleted.name == item.name
    assert had_artifacts is False
    assert not (runtime_root / "work" / item.name).exists()
