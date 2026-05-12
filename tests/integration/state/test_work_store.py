import json
from pathlib import Path

import pytest

import meridian.lib.state.work_store as work_store_module
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.work_store import (
    archive_work_item,
    create_work_item,
    get_work_item,
    list_archived_work_items,
    list_work_items,
    rename_work_item,
    reopen_work_item,
    slugify,
    update_work_item,
    update_work_item_worktree,
)


def _state_root(tmp_path: Path) -> Path:
    # Use a non-.meridian name so _project_paths_for_work_store treats this as a
    # synthetic test root and returns ProjectPaths.from_root_dir() directly,
    # bypassing context resolution.
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def test_slugify_normalizes_and_truncates() -> None:
    assert slugify("Hello_world  2026!!!") == "hello-world-2026"
    assert slugify("___") == ""
    assert slugify("a" * 80) == "a" * 64


def test_rename_work_item_rejects_invalid_name_collision_and_missing_source(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)

    create_work_item(runtime_root, "my feature")
    create_work_item(runtime_root, "beta")

    with pytest.raises(ValueError, match="Invalid work item name"):
        rename_work_item(runtime_root, "my-feature", "Better Name")

    with pytest.raises(ValueError, match="already exists"):
        rename_work_item(runtime_root, "my-feature", "beta")

    with pytest.raises(ValueError, match="not found"):
        rename_work_item(runtime_root, "nonexistent", "new-name")


def test_work_item_archive_and_reopen_preserves_metadata(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)

    item = create_work_item(runtime_root, "My feature")

    assert get_work_item(runtime_root, item.name) is not None
    active_dir = runtime_root / "work" / item.name
    active_status = active_dir / "__status.json"
    assert active_status.exists()
    (active_dir / "notes.md").write_text("hello", encoding="utf-8")

    archived = archive_work_item(runtime_root, item.name)
    archived_dir = runtime_root / "archive" / "work" / item.name
    archived_status = archived_dir / "__status.json"
    assert archived.status == "done"
    assert archived.archived_at is not None
    assert not active_dir.exists()
    assert archived_status.exists()
    assert (archived_dir / "notes.md").read_text(encoding="utf-8") == "hello"

    reopened = reopen_work_item(runtime_root, item.name)
    assert reopened.status == "open"
    assert reopened.archived_at is None
    assert not archived_dir.exists()
    assert active_status.exists()
    assert (active_dir / "notes.md").read_text(encoding="utf-8") == "hello"


def test_work_item_worktree_metadata_uses_nested_schema_and_preserves_branch_on_rename(
    tmp_path: Path,
) -> None:
    runtime_root = _state_root(tmp_path)
    item = create_work_item(runtime_root, "rename-me")

    update_work_item_worktree(
        runtime_root,
        item.name,
        path="/tmp/repo.worktrees/rename-me",
        branch="feature/original-branch",
        pending=True,
    )
    renamed = rename_work_item(runtime_root, item.name, "renamed-item")

    assert renamed.worktree_path == "/tmp/repo.worktrees/rename-me"
    assert renamed.worktree_branch == "feature/original-branch"
    assert renamed.worktree_pending is True

    status_payload = json.loads(
        (runtime_root / "work" / renamed.name / "__status.json").read_text(encoding="utf-8")
    )
    assert "worktree_path" not in status_payload
    assert "worktree_pending" not in status_payload
    assert status_payload["worktree"] == {
        "path": "/tmp/repo.worktrees/rename-me",
        "branch": "feature/original-branch",
        "pending": True,
    }


def test_get_work_item_migrates_legacy_worktree_fields_to_nested_schema(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    item = create_work_item(runtime_root, "legacy-worktree")
    status_path = runtime_root / "work" / item.name / "__status.json"
    status_payload = json.loads(status_path.read_text(encoding="utf-8"))
    status_payload["worktree_path"] = "/tmp/repo.worktrees/legacy-worktree"
    status_payload["worktree_branch"] = "feature/legacy-branch"
    status_payload["worktree_pending"] = True
    status_payload.pop("worktree", None)
    status_path.write_text(json.dumps(status_payload, indent=2) + "\n", encoding="utf-8")

    loaded = get_work_item(runtime_root, item.name)

    assert loaded is not None
    assert loaded.worktree_path == "/tmp/repo.worktrees/legacy-worktree"
    assert loaded.worktree_branch == "feature/legacy-branch"
    assert loaded.worktree_pending is True

    migrated_payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert migrated_payload["worktree"] == {
        "path": "/tmp/repo.worktrees/legacy-worktree",
        "branch": "feature/legacy-branch",
        "pending": True,
    }
    assert "worktree_path" not in migrated_payload
    assert "worktree_pending" not in migrated_payload


def test_list_archived_work_items_repairs_interrupted_archive_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)
    item = create_work_item(runtime_root, "My feature")
    update_work_item(runtime_root, item.name, status="blocked")

    active_dir = paths.work_dir / item.name
    (active_dir / "notes.md").write_text("hello", encoding="utf-8")
    archived_status_path = paths.work_archive_dir / item.name / "__status.json"

    original_atomic_write = work_store_module.atomic_write_text
    failed_once = False

    def crash_during_status_write(path: Path, content: str) -> None:
        nonlocal failed_once
        if path == archived_status_path and not failed_once:
            failed_once = True
            raise OSError("simulated crash after archive move")
        original_atomic_write(path, content)

    monkeypatch.setattr(work_store_module, "atomic_write_text", crash_during_status_write)
    with pytest.raises(OSError, match="simulated crash after archive move"):
        archive_work_item(runtime_root, item.name)

    archived_dir = paths.work_archive_dir / item.name
    assert archived_dir.exists()
    assert not active_dir.exists()
    stale_payload = json.loads(archived_status_path.read_text(encoding="utf-8"))
    assert stale_payload["status"] == "blocked"
    assert stale_payload["archived_at"] is None

    repaired, _ = list_archived_work_items(runtime_root, all_archived=True)
    assert len(repaired) == 1
    assert repaired[0].status == "done"
    assert repaired[0].archived_at is not None

    persisted = get_work_item(runtime_root, item.name)
    assert persisted is not None
    assert persisted.status == "done"
    assert persisted.archived_at is not None


def test_archive_and_reopen_use_context_archive_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "repo"
    runtime_root = project_root / ".meridian"
    user_state_root = tmp_path / "user-state"
    project_root.mkdir()
    user_state_root.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", user_state_root.as_posix())
    monkeypatch.delenv("MERIDIAN_CONFIG", raising=False)
    (project_root / ".git").write_text("gitdir: .git/worktrees/repo\n", encoding="utf-8")
    runtime_root.mkdir(parents=True, exist_ok=True)
    (project_root / "meridian.local.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                'path = "external/work"',
                'archive = "external/archive/work"',
                "",
                "[context.kb]",
                'path = "external/kb"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    item = create_work_item(runtime_root, "My feature")
    active_dir = project_root / "external" / "work" / item.name
    active_dir.mkdir(parents=True, exist_ok=True)
    (active_dir / "notes.md").write_text("hello", encoding="utf-8")

    archive_work_item(runtime_root, item.name)
    archived_dir = project_root / "external" / "archive" / "work" / item.name
    assert not active_dir.exists()
    assert (archived_dir / "notes.md").read_text(encoding="utf-8") == "hello"

    reopen_work_item(runtime_root, item.name)
    assert not archived_dir.exists()
    assert (active_dir / "notes.md").read_text(encoding="utf-8") == "hello"


def test_create_archive_and_reopen_use_project_templated_context_work_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "repo"
    runtime_root = project_root / ".meridian"
    user_state_root = tmp_path / "user-state"
    project_root.mkdir()
    user_state_root.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", user_state_root.as_posix())
    monkeypatch.delenv("MERIDIAN_CONFIG", raising=False)
    (project_root / ".git").write_text("gitdir: .git/worktrees/repo\n", encoding="utf-8")
    runtime_root.mkdir(parents=True, exist_ok=True)
    (project_root / "meridian.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                'path = "contexts/{project}/work"',
                'archive = "contexts/{project}/archive/work"',
                "",
                "[context.kb]",
                'path = "contexts/{project}/kb"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert not (runtime_root / "id").exists()

    item = create_work_item(runtime_root, "My feature")
    project_uuid = (runtime_root / "id").read_text(encoding="utf-8").strip()
    active_dir = project_root / "contexts" / project_uuid / "work" / item.name
    archived_dir = project_root / "contexts" / project_uuid / "archive" / "work" / item.name

    assert project_uuid
    assert active_dir.is_dir()
    assert not (runtime_root / "work" / item.name).exists()

    listed, warnings = list_work_items(runtime_root)
    assert warnings == []
    assert [work.name for work in listed] == [item.name]

    (active_dir / "notes.md").write_text("hello", encoding="utf-8")

    archived = archive_work_item(runtime_root, item.name)
    assert archived.status == "done"
    assert not active_dir.exists()
    assert (archived_dir / "notes.md").read_text(encoding="utf-8") == "hello"

    reopened = reopen_work_item(runtime_root, item.name)
    assert reopened.status == "open"
    assert reopened.archived_at is None
    assert not archived_dir.exists()
    assert (active_dir / "notes.md").read_text(encoding="utf-8") == "hello"


def test_list_work_items_detects_manual_work_directory(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    manual_dir = runtime_root / "work" / "manual-item"
    manual_dir.mkdir(parents=True, exist_ok=True)

    items, _ = list_work_items(runtime_root)

    assert [item.name for item in items] == ["manual-item"]
    assert items[0].status == "open"
    status_payload = json.loads((manual_dir / "__status.json").read_text(encoding="utf-8"))
    assert status_payload["status"] == "open"
    assert status_payload["archived_at"] is None


def test_list_archived_work_items_honors_limit_and_all_archived(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    created = [create_work_item(runtime_root, f"done-item-{idx}") for idx in range(1, 4)]
    for item in created:
        archive_work_item(runtime_root, item.name)

    limited, _ = list_archived_work_items(runtime_root, limit=2)
    all_items, _ = list_archived_work_items(runtime_root, limit=2, all_archived=True)

    assert len(limited) == 2
    assert len(all_items) == 3
    assert {item.name for item in all_items} == {"done-item-1", "done-item-2", "done-item-3"}
    assert {item.name for item in limited}.issubset({item.name for item in all_items})


def test_rename_work_item_keeps_archived_item_archived(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)
    item = create_work_item(runtime_root, "rename-me")
    archive_work_item(runtime_root, item.name)

    renamed = rename_work_item(runtime_root, item.name, "renamed-archived")

    assert renamed.name == "renamed-archived"
    assert renamed.status == "done"
    assert renamed.archived_at is not None
    assert not (paths.work_archive_dir / item.name).exists()
    assert (paths.work_archive_dir / "renamed-archived").is_dir()
    assert get_work_item(runtime_root, item.name) is None
    loaded = get_work_item(runtime_root, "renamed-archived")
    assert loaded is not None
    assert loaded.status == "done"


def test_list_work_items_warns_on_duplicate_in_archive(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)

    create_work_item(runtime_root, "dupe-item")
    # Manually create the same name in archive to simulate the bad state
    archive_dir = paths.work_archive_dir / "dupe-item"
    archive_dir.mkdir(parents=True, exist_ok=True)

    items, warnings = list_work_items(runtime_root)
    assert any(item.name == "dupe-item" for item in items)
    assert len(warnings) == 1
    assert "dupe-item" in warnings[0]
    assert "both active and archive" in warnings[0]

    # Archived listing skips the duplicate and also warns
    archived_items, archived_warnings = list_archived_work_items(runtime_root, all_archived=True)
    assert not any(item.name == "dupe-item" for item in archived_items)
    assert len(archived_warnings) == 1
    assert "dupe-item" in archived_warnings[0]


def test_update_work_item_rejects_done_status(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    item = create_work_item(runtime_root, "cannot-done-via-update")

    with pytest.raises(ValueError, match=r"'done' is reserved for archived work items\."):
        update_work_item(runtime_root, item.name, status="done")
