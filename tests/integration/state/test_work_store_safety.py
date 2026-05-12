from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

import pytest

import meridian.lib.state.work_store as work_store
from meridian.lib.state.paths import RuntimePaths


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


def test_create_work_item_rejects_existing_slug(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)

    work_store.create_work_item(runtime_root, "Shared task")

    with pytest.raises(ValueError, match="already exists"):
        work_store.create_work_item(runtime_root, "Shared task")


def test_rename_work_item_moves_archived_scratch_dir(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)

    item = work_store.create_work_item(runtime_root, "old-name")
    work_store.archive_work_item(runtime_root, item.name)
    archived_dir = paths.work_archive_dir / item.name
    (archived_dir / "notes.md").write_text("archived", encoding="utf-8")

    renamed = work_store.rename_work_item(runtime_root, item.name, "new-name")

    assert renamed.name == "new-name"
    assert not archived_dir.exists()
    assert (paths.work_archive_dir / "new-name" / "notes.md").read_text(
        encoding="utf-8"
    ) == "archived"


def test_update_work_item_not_found_raises_value_error(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)

    with pytest.raises(ValueError, match="not found"):
        work_store.update_work_item(runtime_root, "missing-work")


def test_get_work_item_auto_creates_missing_status_file(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    item = work_store.create_work_item(runtime_root, "repair-me")

    status_path = runtime_root / "work" / item.name / "__status.json"
    status_path.unlink()

    loaded = work_store.get_work_item(runtime_root, item.name)
    assert loaded is not None
    assert loaded.name == item.name
    assert loaded.status == "open"
    assert loaded.archived_at is None

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["status"] == "open"
    assert payload["archived_at"] is None


def test_get_work_item_auto_recreates_malformed_status_file(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    item = work_store.create_work_item(runtime_root, "repair-malformed")

    status_path = runtime_root / "work" / item.name / "__status.json"
    status_path.write_text("not json", encoding="utf-8")

    loaded = work_store.get_work_item(runtime_root, item.name)
    assert loaded is not None
    assert loaded.name == item.name
    assert loaded.status == "open"

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["status"] == "open"
    assert payload["created_at"]
    assert payload["archived_at"] is None


def test_get_work_item_auto_heals_archived_item_missing_archived_at(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    item = work_store.create_work_item(runtime_root, "archive-heal")
    work_store.archive_work_item(runtime_root, item.name)

    archived_status = runtime_root / "archive" / "work" / item.name / "__status.json"
    payload = json.loads(archived_status.read_text(encoding="utf-8"))
    payload["archived_at"] = None
    payload["status"] = "blocked"
    archived_status.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    healed = work_store.get_work_item(runtime_root, item.name)
    assert healed is not None
    assert healed.status == "done"
    assert healed.archived_at is not None

    persisted = json.loads(archived_status.read_text(encoding="utf-8"))
    assert persisted["status"] == "done"
    assert isinstance(persisted["archived_at"], str)
    assert persisted["archived_at"]


def test_get_work_item_reads_status_through_symlink(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    item = work_store.create_work_item(runtime_root, "symlinked-status")
    work_dir = runtime_root / "work" / item.name
    status_path = work_dir / "__status.json"
    target_path = work_dir / "status-target.json"

    payload = {
        "status": "blocked",
        "description": "linked",
        "created_at": "2026-01-01T00:00:00Z",
        "archived_at": None,
    }
    target_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    status_path.unlink()

    try:
        status_path.symlink_to(target_path.name)
    except OSError as exc:
        pytest.skip(f"symlink not supported in test environment: {exc}")

    loaded = work_store.get_work_item(runtime_root, item.name)
    assert loaded is not None
    assert loaded.status == "blocked"
    assert loaded.description == "linked"
    assert loaded.created_at == "2026-01-01T00:00:00Z"


def test_delete_without_force_succeeds_for_status_only_directory(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    item = work_store.create_work_item(runtime_root, "delete-me")

    deleted, had_artifacts = work_store.delete_work_item(runtime_root, item.name, force=False)

    assert deleted.name == item.name
    assert had_artifacts is False
    assert not (runtime_root / "work" / item.name).exists()
