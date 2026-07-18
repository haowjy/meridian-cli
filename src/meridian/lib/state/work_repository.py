"""Serialized mutation repository for directory-authoritative work items.

All status read-modify-write operations and directory namespace changes pass
through this module.  Read projections remain in :mod:`work_store` and never
acquire the mutation lock or persist normalization.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from meridian.lib.platform.atomic import atomic_write_text
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.event_store import utc_now_iso
from meridian.lib.state.paths import ProjectPaths, resolve_project_runtime_root_for_write
from meridian.lib.state.work_store import (
    WorkItem,
    WorktreeMetadata,
    _active_dir,
    _archived_dir,
    _has_artifacts,
    _locate_dirs,
    _normalize_goal,
    _normalize_task_dir_path,
    _project_paths_for_work_store,
    _serialize_status,
    _status_path,
    _status_payload,
    _validate_exact_slug,
    _warn_both_locations,
    _work_item_from_dir,
    slugify,
)

T = TypeVar("T")
_NamespaceMutation = Callable[[ProjectPaths, Path | None, Path | None], T]
_ItemMutation = Callable[[WorkItem], WorkItem]


def _mutate_item(runtime_root: Path, work_id: str, mutation: _NamespaceMutation[T]) -> T:
    """Run one work-item mutation while holding the stable store lock.

    The seam deliberately disables reentrancy: a nested mutation could write an
    inner result and then let the outer mutation overwrite it from a stale
    snapshot.  Failing loudly is safer than permitting that composition.
    """

    paths = _project_paths_for_work_store(runtime_root, create_project_uuid=True)
    lock_root = (
        resolve_project_runtime_root_for_write(runtime_root.parent)
        if runtime_root.name == ".meridian"
        else runtime_root
    )
    with lock_file(lock_root / "work-store.flock", reentrant=False):
        active_dir, archived_dir = _locate_dirs(paths, work_id)
        return mutation(paths, active_dir, archived_dir)


def _write_item(work_dir: Path, item: WorkItem) -> None:
    atomic_write_text(
        _status_path(work_dir),
        _serialize_status(
            _status_payload(
                status=item.status,
                description=item.description,
                goal=item.goal,
                created_at=item.created_at,
                archived_at=item.archived_at,
                task_dir=item.task_dir,
                worktree=item.worktree,
            )
        ),
    )


def _mutate_active_item(
    runtime_root: Path,
    work_id: str,
    mutation: _ItemMutation,
) -> WorkItem:
    def mutate(
        _paths: ProjectPaths,
        active_dir: Path | None,
        archived_dir: Path | None,
    ) -> WorkItem:
        _warn_both_locations(work_id, active_dir, archived_dir)
        if active_dir is None:
            if archived_dir is not None:
                raise ValueError(
                    f"Work item '{work_id}' is archived and cannot be updated. Reopen it first."
                )
            raise ValueError(f"Work item '{work_id}' not found")
        updated = mutation(_work_item_from_dir(active_dir, archived=False))
        _write_item(active_dir, updated)
        return updated

    return _mutate_item(runtime_root, work_id, mutate)


def create_work_item(
    runtime_root: Path,
    label: str,
    description: str = "",
    goal: str | None = None,
) -> WorkItem:
    slug = slugify(label)
    normalized_goal = _normalize_goal(goal)
    if not slug:
        raise ValueError("Work item label must contain at least one letter or number.")

    def create(
        paths: ProjectPaths,
        active_dir: Path | None,
        archived_dir: Path | None,
    ) -> WorkItem:
        if active_dir is not None or archived_dir is not None:
            raise ValueError(
                f"Work item '{slug}' already exists. Use `meridian work switch {slug}`."
            )
        active = _active_dir(paths, slug)
        active.mkdir(parents=True, exist_ok=False)
        item = WorkItem(
            name=slug,
            description=description,
            goal=normalized_goal,
            status="open",
            created_at=utc_now_iso(),
            archived_at=None,
            task_dir=None,
            worktree=WorktreeMetadata(),
        )
        _write_item(active, item)
        return item

    return _mutate_item(runtime_root, slug, create)


def ensure_work_item_metadata(
    runtime_root: Path,
    work_id: str,
    *,
    description: str = "",
    goal: str | None = None,
    status: str = "open",
) -> WorkItem:
    normalized = _validate_exact_slug(work_id)
    normalized_goal = _normalize_goal(goal)
    if status == "done":
        raise ValueError("'done' is reserved for archived work items.")

    def ensure(
        paths: ProjectPaths,
        active_dir: Path | None,
        archived_dir: Path | None,
    ) -> WorkItem:
        _warn_both_locations(normalized, active_dir, archived_dir)
        if active_dir is not None:
            item = _work_item_from_dir(
                active_dir,
                archived=False,
                default_status=status,
                default_description=description,
                default_goal=normalized_goal,
            )
            _write_item(active_dir, item)
            return item
        if archived_dir is not None:
            item = _work_item_from_dir(
                archived_dir,
                archived=True,
                default_description=description,
                default_goal=normalized_goal,
            )
            _write_item(archived_dir, item)
            return item
        created_dir = _active_dir(paths, normalized)
        created_dir.mkdir(parents=True, exist_ok=False)
        item = _work_item_from_dir(
            created_dir,
            archived=False,
            default_status=status,
            default_description=description,
            default_goal=normalized_goal,
        )
        _write_item(created_dir, item)
        return item

    return _mutate_item(runtime_root, normalized, ensure)


def heal_work_item(runtime_root: Path, work_id: str) -> WorkItem:
    """Persist the normalized projection of one existing item under lock."""

    def heal(
        _paths: ProjectPaths,
        active_dir: Path | None,
        archived_dir: Path | None,
    ) -> WorkItem:
        _warn_both_locations(work_id, active_dir, archived_dir)
        work_dir = active_dir or archived_dir
        if work_dir is None:
            raise ValueError(f"Work item '{work_id}' not found")
        item = _work_item_from_dir(work_dir, archived=active_dir is None)
        _write_item(work_dir, item)
        return item

    return _mutate_item(runtime_root, work_id, heal)


def update_work_item(
    runtime_root: Path,
    work_id: str,
    *,
    status: str | None = None,
    description: str | None = None,
    goal: str | None = None,
) -> WorkItem:
    normalized_goal = _normalize_goal(goal)

    def update(current: WorkItem) -> WorkItem:
        next_status = current.status if status is None else status
        if next_status == "done":
            raise ValueError("'done' is reserved for archived work items.")
        return current.model_copy(
            update={
                "status": next_status,
                "description": current.description if description is None else description,
                "goal": current.goal if goal is None else normalized_goal,
                "archived_at": None,
            }
        )

    return _mutate_active_item(runtime_root, work_id, update)


def update_work_item_task_dir(
    runtime_root: Path,
    work_id: str,
    *,
    task_dir: str | None,
) -> WorkItem:
    normalized_task_dir = None
    if task_dir is not None and (stripped := task_dir.strip()):
        normalized_task_dir = _normalize_task_dir_path(stripped)
    return _mutate_active_item(
        runtime_root,
        work_id,
        lambda current: current.model_copy(update={"task_dir": normalized_task_dir}),
    )


def archive_work_item(
    runtime_root: Path,
    work_id: str,
    *,
    description: str | None = None,
) -> WorkItem:
    def archive(
        paths: ProjectPaths,
        active_dir: Path | None,
        archived_dir: Path | None,
    ) -> WorkItem:
        if active_dir is not None and archived_dir is not None:
            _warn_both_locations(work_id, active_dir, archived_dir)
            shutil.rmtree(archived_dir)
            archived_dir = None
        if active_dir is None:
            if archived_dir is not None:
                raise ValueError(f"Work item '{work_id}' is already archived.")
            raise ValueError(f"Work item '{work_id}' not found")

        current = _work_item_from_dir(active_dir, archived=False)
        archived_item = current.model_copy(
            update={
                "description": current.description if description is None else description,
                "status": "done",
                "archived_at": utc_now_iso(),
                "worktree": current.worktree.model_copy(update={"pending": False}),
            }
        )
        # Persist before the location-first authority change. A crash before the
        # rename leaves a healable active item; a crash after it is archived.
        _write_item(active_dir, archived_item)
        destination = _archived_dir(paths, work_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        active_dir.rename(destination)
        return archived_item

    return _mutate_item(runtime_root, work_id, archive)


def reopen_work_item(runtime_root: Path, work_id: str, *, status: str = "open") -> WorkItem:
    if status == "done":
        raise ValueError("'done' is reserved for archived work items.")

    def reopen(
        paths: ProjectPaths,
        active_dir: Path | None,
        archived_dir: Path | None,
    ) -> WorkItem:
        if active_dir is not None and archived_dir is not None:
            _warn_both_locations(work_id, active_dir, archived_dir)
            shutil.rmtree(archived_dir)
            return _work_item_from_dir(active_dir, archived=False)
        if archived_dir is None:
            if active_dir is not None:
                raise ValueError(f"Work item '{work_id}' is already active.")
            raise ValueError(f"Work item '{work_id}' not found")

        current = _work_item_from_dir(archived_dir, archived=True)
        reopened = current.model_copy(
            update={
                "status": status,
                "archived_at": None,
                "worktree": current.worktree.model_copy(update={"pending": False}),
            }
        )
        target = _active_dir(paths, work_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        archived_dir.rename(target)
        _write_item(target, reopened)
        return reopened

    return _mutate_item(runtime_root, work_id, reopen)


def rename_work_item(runtime_root: Path, old_work_id: str, new_name: str) -> WorkItem:
    normalized = _validate_exact_slug(new_name)

    def rename(
        paths: ProjectPaths,
        active_dir: Path | None,
        archived_dir: Path | None,
    ) -> WorkItem:
        if active_dir is not None and archived_dir is not None:
            _warn_both_locations(old_work_id, active_dir, archived_dir)
            shutil.rmtree(archived_dir)
            archived_dir = None
        source = active_dir or archived_dir
        if source is None:
            raise ValueError(f"Work item '{old_work_id}' not found")
        if normalized == old_work_id:
            return _work_item_from_dir(source, archived=active_dir is None)
        if _active_dir(paths, normalized).exists() or _archived_dir(paths, normalized).exists():
            raise ValueError(f"Work item '{normalized}' already exists.")
        target = (
            _active_dir(paths, normalized)
            if active_dir is not None
            else _archived_dir(paths, normalized)
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        return _work_item_from_dir(target, archived=active_dir is None)

    return _mutate_item(runtime_root, old_work_id, rename)


def delete_work_item(
    runtime_root: Path,
    work_id: str,
    *,
    force: bool = False,
) -> tuple[WorkItem, bool]:
    def delete(
        _paths: ProjectPaths,
        active_dir: Path | None,
        archived_dir: Path | None,
    ) -> tuple[WorkItem, bool]:
        primary_dir = active_dir or archived_dir
        if primary_dir is None:
            raise ValueError(f"Work item '{work_id}' not found")
        deleted_item = _work_item_from_dir(primary_dir, archived=active_dir is None)
        existing_dirs = [path for path in (active_dir, archived_dir) if path is not None]
        had_artifacts = any(_has_artifacts(path) for path in existing_dirs)
        if had_artifacts and not force:
            raise ValueError(f"Work item '{work_id}' has artifacts. Use --force to delete.")
        for work_dir in existing_dirs:
            shutil.rmtree(work_dir)
        return deleted_item, had_artifacts

    return _mutate_item(runtime_root, work_id, delete)
