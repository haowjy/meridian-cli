"""Pure read projections for directory-authoritative work items.

Reads never mutate metadata. Directory location remains the sole authority for
whether an item is active or archived.
"""

from __future__ import annotations

from pathlib import Path

from meridian.lib.state.work_state import (
    WorkItem,
    active_work_dir,
    archived_work_dir,
    load_work_item_from_dir,
    locate_work_dirs,
    project_paths_for_work_store,
    serialize_work_item_state,
    slugify,
    status_path,
    stored_state_from_item,
    warn_both_locations,
)


def _is_valid_work_slug(name: str) -> bool:
    """Return True if ``name`` is a valid work-item slug (survives slugify unchanged)."""
    return bool(name) and slugify(name) == name


def _list_work_item_dirs(root_dir: Path) -> list[Path]:
    if not root_dir.is_dir():
        return []
    return [
        child for child in root_dir.iterdir() if child.is_dir() and _is_valid_work_slug(child.name)
    ]


def _normalize_worktree_path(path: str) -> str:
    try:
        return Path(path).expanduser().resolve().as_posix()
    except OSError:
        return Path(path).expanduser().as_posix()


def work_item_needs_repair(runtime_root: Path, work_id: str) -> bool:
    """Return whether an existing item's persisted metadata is non-canonical."""

    paths = project_paths_for_work_store(runtime_root)
    active_dir, archived_dir = locate_work_dirs(paths, work_id)
    warn_both_locations(work_id, active_dir, archived_dir)
    work_dir = active_dir or archived_dir
    if work_dir is None:
        return False
    item = load_work_item_from_dir(work_dir, archived=active_dir is None)
    expected = serialize_work_item_state(stored_state_from_item(item))
    try:
        return status_path(work_dir).read_text(encoding="utf-8") != expected
    except OSError:
        return True


def get_work_item(runtime_root: Path, work_id: str) -> WorkItem | None:
    """Load one work item from active or archived directories."""

    paths = project_paths_for_work_store(runtime_root)
    active_dir, archived_dir = locate_work_dirs(paths, work_id)
    warn_both_locations(work_id, active_dir, archived_dir)
    if active_dir is not None:
        return load_work_item_from_dir(active_dir, archived=False)
    if archived_dir is not None:
        return load_work_item_from_dir(archived_dir, archived=True)
    return None


def get_active_work_item(
    runtime_root: Path,
    work_id: str,
) -> WorkItem | None:
    """Load one active work item."""

    paths = project_paths_for_work_store(runtime_root)
    active_dir = active_work_dir(paths, work_id)
    if not active_dir.is_dir():
        return None
    return load_work_item_from_dir(active_dir, archived=False)


def work_scratch_dir(runtime_root: Path, work_id: str) -> Path:
    """Return current active/archive work directory if present, otherwise active path."""

    paths = project_paths_for_work_store(runtime_root)
    active_dir, archived_dir = locate_work_dirs(paths, work_id)
    warn_both_locations(work_id, active_dir, archived_dir)
    if active_dir is not None:
        return active_dir
    if archived_dir is not None:
        return archived_dir
    return active_work_dir(paths, work_id)


def list_work_items(runtime_root: Path) -> tuple[list[WorkItem], list[str]]:
    """Return active work items sorted by (created_at, name) and any warnings.

    Items that exist in both active and archive directories are included from
    the active directory with a warning rather than raising.
    """

    paths = project_paths_for_work_store(runtime_root)
    active_dirs = _list_work_item_dirs(paths.work_dir)
    if not active_dirs:
        return [], []
    archived_names = {child.name for child in _list_work_item_dirs(paths.work_archive_dir)}

    items: list[WorkItem] = []
    warnings: list[str] = []
    for child in active_dirs:
        if child.name in archived_names:
            warnings.append(
                f"Work item '{child.name}' exists in both active and archive directories. "
                "Remove one to resolve: "
                f"active={child}, archive={archived_work_dir(paths, child.name)}"
            )
        items.append(load_work_item_from_dir(child, archived=False))
    return sorted(items, key=lambda item: (item.created_at, item.name)), warnings


def list_archived_work_items(
    runtime_root: Path,
    *,
    limit: int = 10,
    all_archived: bool = False,
) -> tuple[list[WorkItem], list[str]]:
    """Return archived work items sorted by archived_at descending and any warnings.

    Items that exist in both active and archive directories are skipped from
    the archived listing (the active copy takes precedence) with a warning.
    """

    paths = project_paths_for_work_store(runtime_root)
    archived_dirs = _list_work_item_dirs(paths.work_archive_dir)
    if not archived_dirs:
        return [], []

    if limit < 0:
        raise ValueError("limit must be non-negative.")
    active_names = {child.name for child in _list_work_item_dirs(paths.work_dir)}

    items: list[WorkItem] = []
    warnings: list[str] = []
    for child in archived_dirs:
        if child.name in active_names:
            warnings.append(
                f"Work item '{child.name}' exists in both active and archive directories. "
                "Remove one to resolve: "
                f"active={active_work_dir(paths, child.name)}, archive={child}"
            )
            continue
        items.append(load_work_item_from_dir(child, archived=True))

    items.sort(
        key=lambda item: (
            item.archived_at is not None,
            item.archived_at or "",
            item.name,
        ),
        reverse=True,
    )
    if all_archived:
        return items, warnings
    return items[:limit], warnings


def list_work_item_references_for_path(
    runtime_root: Path,
    worktree_path: str,
    *,
    exclude_work_id: str | None = None,
    include_archived: bool = True,
) -> list[WorkItem]:
    """Return work items that reference ``worktree_path``."""

    target = _normalize_worktree_path(worktree_path)
    active_items, _ = list_work_items(runtime_root)
    archived_items: list[WorkItem] = []
    if include_archived:
        archived_items, _ = list_archived_work_items(runtime_root, all_archived=True)
    matches: list[WorkItem] = []
    for item in (*active_items, *archived_items):
        if exclude_work_id is not None and item.name == exclude_work_id:
            continue
        if item.worktree_path is None:
            continue
        if _normalize_worktree_path(item.worktree_path) == target:
            matches.append(item)
    return matches
