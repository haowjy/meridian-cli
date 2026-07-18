import json
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("status", ["", "   ", "done"])
def test_update_rejects_invalid_active_status(tmp_path: Path, status: str) -> None:
    runtime_root = _state_root(tmp_path)
    item = create_work_item(runtime_root, "status-validation")
    status_path = runtime_root / "work" / item.name / "__status.json"
    original = status_path.read_bytes()

    with pytest.raises(ValueError):
        update_work_item(runtime_root, item.name, status=status)

    assert status_path.read_bytes() == original


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
def test_list_archived_work_items_projects_stale_status_without_persisting(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)
    item = create_work_item(runtime_root, "My feature")
    update_work_item(runtime_root, item.name, status="blocked")

    active_dir = paths.work_dir / item.name
    (active_dir / "notes.md").write_text("hello", encoding="utf-8")
    archived_dir = paths.work_archive_dir / item.name
    archived_dir.parent.mkdir(parents=True, exist_ok=True)
    active_dir.rename(archived_dir)
    archived_status_path = archived_dir / "__status.json"
    stale_payload = json.loads(archived_status_path.read_text(encoding="utf-8"))
    assert stale_payload["status"] == "blocked"
    assert stale_payload["archived_at"] is None

    repaired, _ = list_archived_work_items(runtime_root, all_archived=True)
    assert len(repaired) == 1
    assert repaired[0].status == "done"
    assert repaired[0].archived_at is not None

    assert json.loads(archived_status_path.read_text(encoding="utf-8")) == stale_payload
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
