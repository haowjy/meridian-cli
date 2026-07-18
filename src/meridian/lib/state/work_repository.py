"""Serialized mutation repository for directory-authoritative work items.

All status read-modify-write operations and directory namespace changes pass
through this module.  Read projections remain in :mod:`work_store` and never
acquire the mutation lock or persist normalization.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from meridian.lib.platform.atomic import atomic_write_text
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.event_store import utc_now_iso
from meridian.lib.state.paths import ProjectPaths
from meridian.lib.state.work_state import (
    STATUS_FILENAME,
    WorkItem,
    WorktreeMetadata,
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

T = TypeVar("T")
_StateWrite = tuple[Path, WorkItem]


@dataclass(frozen=True)
class _MutationResult(Generic[T]):
    value: T
    state_write: _StateWrite | None = None


_NamespaceMutation = Callable[[ProjectPaths, Path | None, Path | None], _MutationResult[T]]
_ItemMutation = Callable[[WorkItem], WorkItem]


def _validate_active_status(status: str) -> str:
    if not status.strip():
        raise ValueError("Work item status must not be empty.")
    if status == "done":
        raise ValueError("'done' is reserved for archived work items.")
    return status


def _has_artifacts(work_dir: Path) -> bool:
    if not work_dir.is_dir():
        return False
    return any(child.name != STATUS_FILENAME for child in work_dir.iterdir())


def _validate_exact_slug(raw_name: str) -> str:
    normalized = slugify(raw_name)
    if not normalized or normalized != raw_name:
        raise ValueError(
            f"Invalid work item name '{raw_name}'. "
            f"Use a slug (lowercase, hyphens, no spaces) — e.g. '{normalized or 'my-feature'}'."
        )
    return normalized


def _normalize_goal(goal: str | None) -> str | None:
    if goal is None:
        return None
    return goal.strip() or None


def _normalize_task_dir_path(path: str) -> str:
    try:
        return Path(path).expanduser().resolve().as_posix()
    except OSError:
        return Path(path).expanduser().as_posix()


def write_state_locked(runtime_root: Path, work_id: str, mutation: _NamespaceMutation[T]) -> T:
    """Run one work mutation and its optional atomic state write under one lock.

    The seam deliberately disables reentrancy: a nested mutation could write an
    inner result and then let the outer mutation overwrite it from a stale
    snapshot.  Failing loudly is safer than permitting that composition.
    """

    paths = project_paths_for_work_store(runtime_root, create_project_uuid=True)
    with lock_file(paths.root_dir / "work-store.flock", reentrant=False):
        active_dir, archived_dir = locate_work_dirs(paths, work_id)
        result = mutation(paths, active_dir, archived_dir)
        if result.state_write is not None:
            work_dir, item = result.state_write
            _write_item(work_dir, item)
        return result.value


def _write_item(work_dir: Path, item: WorkItem) -> None:
    atomic_write_text(
        status_path(work_dir),
        serialize_work_item_state(stored_state_from_item(item)),
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
    ) -> _MutationResult[WorkItem]:
        warn_both_locations(work_id, active_dir, archived_dir)
        if active_dir is None:
            if archived_dir is not None:
                raise ValueError(
                    f"Work item '{work_id}' is archived and cannot be updated. Reopen it first."
                )
            raise ValueError(f"Work item '{work_id}' not found")
        updated = mutation(load_work_item_from_dir(active_dir, archived=False))
        return _MutationResult(updated, (active_dir, updated))

    return write_state_locked(runtime_root, work_id, mutate)


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
    ) -> _MutationResult[WorkItem]:
        if active_dir is not None or archived_dir is not None:
            raise ValueError(
                f"Work item '{slug}' already exists. Use `meridian work switch {slug}`."
            )
        active = active_work_dir(paths, slug)
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
        return _MutationResult(item, (active, item))

    return write_state_locked(runtime_root, slug, create)


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
    status = _validate_active_status(status)

    def ensure(
        paths: ProjectPaths,
        active_dir: Path | None,
        archived_dir: Path | None,
    ) -> _MutationResult[WorkItem]:
        warn_both_locations(normalized, active_dir, archived_dir)
        if active_dir is not None:
            item = load_work_item_from_dir(
                active_dir,
                archived=False,
                default_status=status,
                default_description=description,
                default_goal=normalized_goal,
            )
            return _MutationResult(item, (active_dir, item))
        if archived_dir is not None:
            item = load_work_item_from_dir(
                archived_dir,
                archived=True,
                default_description=description,
                default_goal=normalized_goal,
            )
            return _MutationResult(item, (archived_dir, item))
        created_dir = active_work_dir(paths, normalized)
        created_dir.mkdir(parents=True, exist_ok=False)
        item = load_work_item_from_dir(
            created_dir,
            archived=False,
            default_status=status,
            default_description=description,
            default_goal=normalized_goal,
        )
        return _MutationResult(item, (created_dir, item))

    return write_state_locked(runtime_root, normalized, ensure)


def repair_work_item(runtime_root: Path, work_id: str) -> WorkItem:
    """Persist a location-authoritative projection of one item under lock."""

    def repair(
        _paths: ProjectPaths,
        active_dir: Path | None,
        archived_dir: Path | None,
    ) -> _MutationResult[WorkItem]:
        warn_both_locations(work_id, active_dir, archived_dir)
        work_dir = active_dir or archived_dir
        if work_dir is None:
            raise ValueError(f"Work item '{work_id}' not found")
        item = load_work_item_from_dir(work_dir, archived=active_dir is None)
        return _MutationResult(item, (work_dir, item))

    return write_state_locked(runtime_root, work_id, repair)


def update_work_item(
    runtime_root: Path,
    work_id: str,
    *,
    status: str | None = None,
    description: str | None = None,
    goal: str | None = None,
) -> WorkItem:
    normalized_goal = _normalize_goal(goal)
    if status is not None:
        status = _validate_active_status(status)

    def update(current: WorkItem) -> WorkItem:
        next_status = current.status if status is None else status
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
    ) -> _MutationResult[WorkItem]:
        if active_dir is not None and archived_dir is not None:
            warn_both_locations(work_id, active_dir, archived_dir)
            shutil.rmtree(archived_dir)
            archived_dir = None
        if active_dir is None:
            if archived_dir is not None:
                raise ValueError(f"Work item '{work_id}' is already archived.")
            raise ValueError(f"Work item '{work_id}' not found")

        current = load_work_item_from_dir(active_dir, archived=False)
        archived_item = current.model_copy(
            update={
                "description": current.description if description is None else description,
                "status": "done",
                "archived_at": utc_now_iso(),
                "worktree": current.worktree.model_copy(update={"pending": False}),
            }
        )
        destination = archived_work_dir(paths, work_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        active_dir.rename(destination)
        return _MutationResult(archived_item, (destination, archived_item))

    return write_state_locked(runtime_root, work_id, archive)


def reopen_work_item(runtime_root: Path, work_id: str, *, status: str = "open") -> WorkItem:
    status = _validate_active_status(status)

    def reopen(
        paths: ProjectPaths,
        active_dir: Path | None,
        archived_dir: Path | None,
    ) -> _MutationResult[WorkItem]:
        if active_dir is not None and archived_dir is not None:
            warn_both_locations(work_id, active_dir, archived_dir)
            shutil.rmtree(archived_dir)
            return _MutationResult(load_work_item_from_dir(active_dir, archived=False))
        if archived_dir is None:
            if active_dir is not None:
                raise ValueError(f"Work item '{work_id}' is already active.")
            raise ValueError(f"Work item '{work_id}' not found")

        current = load_work_item_from_dir(archived_dir, archived=True)
        reopened = current.model_copy(
            update={
                "status": status,
                "archived_at": None,
                "worktree": current.worktree.model_copy(update={"pending": False}),
            }
        )
        target = active_work_dir(paths, work_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        archived_dir.rename(target)
        return _MutationResult(reopened, (target, reopened))

    return write_state_locked(runtime_root, work_id, reopen)


def rename_work_item(runtime_root: Path, old_work_id: str, new_name: str) -> WorkItem:
    normalized = _validate_exact_slug(new_name)

    def rename(
        paths: ProjectPaths,
        active_dir: Path | None,
        archived_dir: Path | None,
    ) -> _MutationResult[WorkItem]:
        if active_dir is not None and archived_dir is not None:
            warn_both_locations(old_work_id, active_dir, archived_dir)
            shutil.rmtree(archived_dir)
            archived_dir = None
        source = active_dir or archived_dir
        if source is None:
            raise ValueError(f"Work item '{old_work_id}' not found")
        if normalized == old_work_id:
            return _MutationResult(load_work_item_from_dir(source, archived=active_dir is None))
        if (
            active_work_dir(paths, normalized).exists()
            or archived_work_dir(paths, normalized).exists()
        ):
            raise ValueError(f"Work item '{normalized}' already exists.")
        target = (
            active_work_dir(paths, normalized)
            if active_dir is not None
            else archived_work_dir(paths, normalized)
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        return _MutationResult(load_work_item_from_dir(target, archived=active_dir is None))

    return write_state_locked(runtime_root, old_work_id, rename)


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
    ) -> _MutationResult[tuple[WorkItem, bool]]:
        primary_dir = active_dir or archived_dir
        if primary_dir is None:
            raise ValueError(f"Work item '{work_id}' not found")
        deleted_item = load_work_item_from_dir(primary_dir, archived=active_dir is None)
        existing_dirs = [path for path in (active_dir, archived_dir) if path is not None]
        had_artifacts = any(_has_artifacts(path) for path in existing_dirs)
        if had_artifacts and not force:
            raise ValueError(f"Work item '{work_id}' has artifacts. Use --force to delete.")
        for work_dir in existing_dirs:
            shutil.rmtree(work_dir)
        return _MutationResult((deleted_item, had_artifacts))

    return write_state_locked(runtime_root, work_id, delete)
