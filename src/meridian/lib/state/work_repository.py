"""Serialized mutation repository for directory-authoritative work items.

All status read-modify-write operations and directory namespace changes pass
through this module.  Read projections remain in :mod:`work_store` and never
acquire the mutation lock or persist normalization.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

import structlog

from meridian.lib.core.logging import warning_cache_scope
from meridian.lib.platform.atomic import atomic_write_text
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.event_store import utc_now_iso
from meridian.lib.state.paths import ProjectPaths, resolve_project_runtime_root_for_write
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

logger = structlog.get_logger(__name__)

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


def _work_runtime_root(project_state_dir: Path) -> Path:
    """Return the user runtime root that holds this work store's project spawns."""

    if project_state_dir.name == ".meridian":
        return resolve_project_runtime_root_for_write(project_state_dir.parent)
    return project_state_dir


def pending_archive_blockers(runtime_root: Path, work_id: str) -> tuple[str, ...]:
    """Return the live spawns that still hold a done item's directory.

    A non-terminal spawn keeps its launch-bound work directory, so moving the item
    out from under it would leave a ghost directory the spawn recreates on its next
    write. Primaries are excluded: ``work done`` detaches them, and their spawn
    rows keep the launch work id.
    """

    from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
    from meridian.lib.state import spawn_store
    from meridian.lib.state.spawn_status import overlay_derived

    rows = overlay_derived(
        runtime_root,
        spawn_store.list_spawns(runtime_root, work_id=work_id).records,
    )
    return tuple(
        row.id
        for row in rows
        if (row.kind or "").strip() != "primary" and is_active_spawn_status(row.status)
    )


def _same_file_contents(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        return left.read_bytes() == right.read_bytes()
    except OSError:
        return False


def _merge_conflicts(source: Path, destination: Path, *, relative: Path) -> list[Path]:
    """List the paths that differ between two copies of one work directory.

    ``__status.json`` never counts: the caller writes the authoritative state after
    the move.
    """

    conflicts: list[Path] = []
    for child in sorted(source.iterdir()):
        if child.name == STATUS_FILENAME:
            continue
        target = destination / child.name
        rel = relative / child.name
        if not target.exists() and not target.is_symlink():
            continue
        if child.is_symlink() or target.is_symlink():
            same_link = child.is_symlink() and target.is_symlink() and (
                os.readlink(child) == os.readlink(target)
            )
            if not same_link:
                conflicts.append(rel)
            continue
        if child.is_dir() and target.is_dir():
            conflicts.extend(_merge_conflicts(child, target, relative=rel))
        elif child.is_file() and target.is_file():
            if not _same_file_contents(child, target):
                conflicts.append(rel)
        else:
            conflicts.append(rel)
    return conflicts


def _merge_into(source: Path, destination: Path) -> None:
    """Move *source* into *destination*, keeping every distinct file.

    Only call after :func:`_merge_conflicts` returned nothing: shared paths are
    identical, so dropping the source's copy of one loses no content.
    """

    for child in sorted(source.iterdir()):
        if child.name == STATUS_FILENAME:
            continue
        target = destination / child.name
        counterpart = target.exists() or target.is_symlink()
        if child.is_symlink():
            if counterpart:
                child.unlink()
            else:
                child.rename(target)
            continue
        if child.is_file() and counterpart:
            child.unlink()
            continue
        if child.is_dir() and target.is_dir():
            _merge_into(child, target)
            continue
        child.rename(target)
    (source / STATUS_FILENAME).unlink(missing_ok=True)
    source.rmdir()


def move_work_dir(
    source: Path,
    destination: Path,
    *,
    before_move: Callable[[], None] | None = None,
) -> None:
    """Move one work directory between the active and archived locations.

    A spawn that still held the old path can recreate it, so both copies may exist
    when the move finally runs. Merge the source into the destination when the
    source only adds files, and refuse otherwise: silently losing one copy is worse
    than stopping with both paths named. ``before_move`` runs after that check and
    immediately before the first rename, so a refused move leaves the source's
    metadata as it was.
    """

    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        if before_move is not None:
            before_move()
        source.rename(destination)
        return
    conflicts = _merge_conflicts(source, destination, relative=Path())
    if conflicts:
        shown = ", ".join(path.as_posix() for path in conflicts[:5])
        more = f" (and {len(conflicts) - 5} more)" if len(conflicts) > 5 else ""
        raise ValueError(
            f"Both {source.as_posix()} and {destination.as_posix()} exist and differ at "
            f"{shown}{more}. Nothing was moved or deleted: reconcile the two directories "
            "and re-run."
        )
    if before_move is not None:
        before_move()
    _merge_into(source, destination)


def write_state_locked(runtime_root: Path, work_id: str, mutation: _NamespaceMutation[T]) -> T:
    """Run one work mutation and its optional atomic state write under one lock.

    The seam deliberately disables reentrancy: a nested mutation could write an
    inner result and then let the outer mutation overwrite it from a stale
    snapshot.  Failing loudly is safer than permitting that composition.
    """

    paths = project_paths_for_work_store(runtime_root, create_project_uuid=True)
    lock_root = _work_runtime_root(runtime_root)
    with (
        warning_cache_scope(),
        lock_file(lock_root / "work-store.flock", reentrant=False),
    ):
        active_dir, archived_dir = locate_work_dirs(paths, work_id)
        # Validate every copy before the mutation can rename, delete, or rewrite
        # either directory. Duplicate-location cleanup must not bypass this gate.
        if active_dir is not None:
            load_work_item_from_dir(active_dir, archived=False)
        if archived_dir is not None:
            load_work_item_from_dir(archived_dir, archived=True)
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
    """Mark an item done now and move its directory to the archive.

    The move waits while any non-terminal spawn is still attached: those spawns hold
    the item's directory as their launch-bound work directory, so moving it would
    leave a ghost they recreate on the next write. The done status is written first,
    so a crash leaves a pending move the startup sweep finishes.
    """

    project_runtime_root = _work_runtime_root(runtime_root)

    def archive(
        paths: ProjectPaths,
        active_dir: Path | None,
        archived_dir: Path | None,
    ) -> _MutationResult[WorkItem]:
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
        if pending_archive_blockers(project_runtime_root, work_id):
            _write_item(active_dir, archived_item)
            return _MutationResult(archived_item)
        destination = archived_work_dir(paths, work_id)
        move_work_dir(
            active_dir,
            destination,
            before_move=lambda: _write_item(active_dir, archived_item),
        )
        return _MutationResult(archived_item, (destination, archived_item))

    return write_state_locked(runtime_root, work_id, archive)


def reopen_work_item(runtime_root: Path, work_id: str, *, status: str = "open") -> WorkItem:
    """Cancel a deferred archive move, or move the archived directory back to work/.

    The one implementation behind ``work reopen`` and ``work start`` on a done item.
    A done item still in the active directory never moved, so reopening only rewrites
    its status. An archived item moves back under the same rules as the archive move:
    merge a copy a spawn recreated when it only adds files, otherwise refuse with both
    paths named.
    """

    status = _validate_active_status(status)

    def reopened_item(current: WorkItem) -> WorkItem:
        return current.model_copy(
            update={
                "status": status,
                "archived_at": None,
                "worktree": current.worktree.model_copy(update={"pending": False}),
            }
        )

    def reopen(
        paths: ProjectPaths,
        active_dir: Path | None,
        archived_dir: Path | None,
    ) -> _MutationResult[WorkItem]:
        if archived_dir is None:
            if active_dir is None:
                raise ValueError(f"Work item '{work_id}' not found")
            current = load_work_item_from_dir(active_dir, archived=False)
            if current.status != "done":
                raise ValueError(f"Work item '{work_id}' is already active.")
            reopened = reopened_item(current)
            return _MutationResult(reopened, (active_dir, reopened))

        current = load_work_item_from_dir(archived_dir, archived=True)
        reopened = reopened_item(current)
        destination = active_work_dir(paths, work_id)
        # Intent before the move, and only once the move is known to be allowed: a
        # refused or crashed move leaves the item archived, never done in work/.
        move_work_dir(
            archived_dir,
            destination,
            before_move=lambda: _write_item(archived_dir, reopened),
        )
        return _MutationResult(reopened, (destination, reopened))

    return write_state_locked(runtime_root, work_id, reopen)


def settle_pending_archive(
    runtime_root: Path,
    work_id: str,
) -> WorkItem | None:
    """Finish a deferred archive move once no live spawn still holds the item.

    Reads the done status a deferred ``work done`` wrote and moves the directory when
    the last attached spawn is terminal. Idempotent: with nothing pending it returns
    ``None`` and touches nothing.
    """

    project_runtime_root = _work_runtime_root(runtime_root)

    def settle(
        paths: ProjectPaths,
        active_dir: Path | None,
        archived_dir: Path | None,
    ) -> _MutationResult[WorkItem | None]:
        if active_dir is None:
            return _MutationResult(None)
        item = load_work_item_from_dir(active_dir, archived=False)
        if item.status != "done":
            return _MutationResult(None)
        if pending_archive_blockers(project_runtime_root, work_id):
            return _MutationResult(None)
        destination = archived_work_dir(paths, work_id)
        move_work_dir(active_dir, destination)
        return _MutationResult(item, (destination, item))

    return write_state_locked(runtime_root, work_id, settle)


def settle_pending_moves(runtime_root: Path) -> tuple[str, ...]:
    """Finish every deferred archive move a crash or a finished spawn left behind.

    The startup sweep: cheap enough for a launch, and it never fails the caller. An
    item that cannot be merged keeps both copies and is reported as done until
    ``work done``/``reopen`` resolves it with both paths named.
    """

    paths = project_paths_for_work_store(runtime_root)
    if paths.work_dir is None or not paths.work_dir.is_dir():
        return ()
    settled: list[str] = []
    for child in sorted(paths.work_dir.iterdir()):
        if not child.is_dir() or slugify(child.name) != child.name:
            continue
        try:
            if (child / STATUS_FILENAME).exists():
                if settle_pending_archive(runtime_root, child.name) is not None:
                    settled.append(child.name)
                continue
            # A spawn that held an archived item's old path after the move wrote a
            # directory with no metadata: merge it into the archive it belongs to,
            # once no other spawn is still writing there.
            archived = archived_work_dir(paths, child.name)
            if archived.is_dir() and not pending_archive_blockers(
                _work_runtime_root(runtime_root), child.name
            ):
                move_work_dir(child, archived)
                settled.append(child.name)
        except Exception:
            logger.warning(
                "Could not finish a deferred work archive move.",
                work_id=child.name,
                work_dir=child.as_posix(),
                exc_info=True,
            )
    return tuple(settled)


def rename_work_item(runtime_root: Path, old_work_id: str, new_name: str) -> WorkItem:
    normalized = _validate_exact_slug(new_name)

    def rename(
        paths: ProjectPaths,
        active_dir: Path | None,
        archived_dir: Path | None,
    ) -> _MutationResult[WorkItem]:
        if active_dir is not None and archived_dir is not None:
            raise ValueError(
                f"Work item '{old_work_id}' exists in both {active_dir.as_posix()} and "
                f"{archived_dir.as_posix()}. Nothing was renamed or deleted: reconcile "
                "the two directories first."
            )
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
