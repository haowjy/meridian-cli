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
    return bool(name) and slugify(name) == name


def _list_work_item_dirs(root_dir: Path) -> list[Path]:
    if not root_dir.is_dir():
        return []
    return [
        child for child in root_dir.iterdir() if child.is_dir() and _is_valid_work_slug(child.name)
    ]


def work_item_needs_repair(runtime_root: Path, work_id: str) -> bool:

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
    paths = project_paths_for_work_store(runtime_root)
    return paths.work_dir is not None and any(locate_work_dirs(paths, work_id))


def _recorded_task_dir(item: WorkItem) -> Path | None:
    return Path(item.task_dir).expanduser().resolve() if item.task_dir else None


def item_task_dir(runtime_root: Path, work_id: str) -> Path | None:
    item = get_work_item(runtime_root, work_id)
    return _recorded_task_dir(item) if item is not None else None


def item_checkout(item: WorkItem, project_root: Path) -> Path:
    recorded = _recorded_task_dir(item)
    return recorded if recorded is not None and recorded.is_dir() else project_root


def get_work_item(runtime_root: Path, work_id: str) -> WorkItem | None:

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
    paths = project_paths_for_work_store(runtime_root)
    active_dir, archived_dir = locate_work_dirs(paths, work_id)
    warn_both_locations(work_id, active_dir, archived_dir)
    return active_dir or archived_dir or active_work_dir(paths, work_id)


def list_work_items(runtime_root: Path) -> tuple[list[WorkItem], list[str]]:
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
