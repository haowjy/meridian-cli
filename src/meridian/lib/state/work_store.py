"""Pure read projections for directory-authoritative work items.

Reads never mutate metadata. Directory location remains the sole authority for
whether an item is active or archived.
"""

from __future__ import annotations

from pathlib import Path

from meridian.lib.state.work_state import (
    UnsupportedWorkStateVersion,
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


def work_item_needs_repair(runtime_root: Path, work_id: str) -> bool:
    """Return whether an existing item's persisted metadata is non-canonical."""

    paths = project_paths_for_work_store(runtime_root)
    active_dir, archived_dir = locate_work_dirs(paths, work_id)
    warn_both_locations(work_id, active_dir, archived_dir)
    work_dir = active_dir or archived_dir
    if work_dir is None:
        return False
    try:
        item = load_work_item_from_dir(work_dir, archived=active_dir is None)
    except UnsupportedWorkStateVersion:
        return False
    expected = serialize_work_item_state(stored_state_from_item(item))
    try:
        return status_path(work_dir).read_text(encoding="utf-8") != expected
    except OSError:
        return True


def work_item_exists(runtime_root: Path, work_id: str) -> bool:
    """Return whether an item has a directory in ``work/`` or the archive.

    Two stats and no read: the question a stale session row or env must answer
    before it names an item.
    """

    paths = project_paths_for_work_store(runtime_root)
    return paths.work_dir is not None and any(locate_work_dirs(paths, work_id))


def _recorded_task_dir(item: WorkItem) -> Path | None:
    return Path(item.task_dir).expanduser().resolve() if item.task_dir else None


def item_task_dir(runtime_root: Path, work_id: str) -> Path | None:
    """Return the checkout an item records, whether it is active or done.

    A done item keeps its checkout: its agents are still working there, and its
    ``task`` link keeps naming it.
    """

    item = get_work_item(runtime_root, work_id)
    return _recorded_task_dir(item) if item is not None else None


def item_checkout(item: WorkItem, project_root: Path) -> Path:
    """Return the checkout an item's agents edit: its ``task_dir`` while that is a
    directory, otherwise the project root.

    The one rule behind the item's ``task`` link, for a done item as for an active one.
    A reader that reports a stale ``task_dir`` takes it from :func:`item_task_dir`.
    """

    recorded = _recorded_task_dir(item)
    return recorded if recorded is not None and recorded.is_dir() else project_root


def get_work_item(runtime_root: Path, work_id: str) -> WorkItem | None:
    """Load one work item from active or archived directories."""

    paths = project_paths_for_work_store(runtime_root)
    if paths.work_dir is None or paths.work_archive_dir is None:
        return None
    active_dir, archived_dir = locate_work_dirs(paths, work_id)
    warn_both_locations(work_id, active_dir, archived_dir)
    if active_dir is not None:
        return load_work_item_from_dir(active_dir, archived=False, mark_unsupported=True)
    if archived_dir is not None:
        return load_work_item_from_dir(archived_dir, archived=True, mark_unsupported=True)
    return None


def work_scratch_dir(runtime_root: Path, work_id: str) -> Path:
    """Return current active/archive work directory if present, otherwise active path.

    Location is authoritative: `work done` moves the directory, so a reader that
    assumed the active path would name a directory that is no longer there. An
    unconfigured archive root simply has nothing to find.
    """

    paths = project_paths_for_work_store(runtime_root)
    active_dir, archived_dir = locate_work_dirs(paths, work_id)
    warn_both_locations(work_id, active_dir, archived_dir)
    return active_dir or archived_dir or active_work_dir(paths, work_id)


def list_work_items(runtime_root: Path) -> tuple[list[WorkItem], list[str]]:
    """Return active work items sorted by (created_at, name) and any warnings.

    Items that exist in both active and archive directories are included from
    the active directory with a warning rather than raising.
    """

    paths = project_paths_for_work_store(runtime_root)
    if paths.work_dir is None or paths.work_archive_dir is None:
        return [], []
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
        items.append(load_work_item_from_dir(child, archived=False, mark_unsupported=True))
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
    if paths.work_dir is None or paths.work_archive_dir is None:
        return [], []
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
        items.append(load_work_item_from_dir(child, archived=True, mark_unsupported=True))

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
