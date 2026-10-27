"""Serialized mutation repository for directory-authoritative work items.

All status read-modify-write operations and directory namespace changes pass
through this module.  Read projections remain in :mod:`work_store` and never
acquire the mutation lock or persist normalization.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

import structlog

from meridian.lib.core.logging import warning_cache_scope
from meridian.lib.platform.atomic import atomic_symlink, atomic_write_text
from meridian.lib.platform.locking import lock_file
from meridian.lib.state import work_store
from meridian.lib.state.event_store import utc_now_iso
from meridian.lib.state.paths import (
    ProjectPaths,
    RuntimePaths,
    resolve_project_runtime_root_for_write,
)
from meridian.lib.state.work_state import (
    STATUS_FILENAME,
    WorkItem,
    WorktreeMetadata,
    active_work_dir,
    archived_work_dir,
    load_work_item_from_dir,
    locate_work_dirs,
    project_paths_for_work_store,
    project_root_for_work_store,
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
    # Link directory -> the item whose targets it takes, republished after the
    # mutation. Default: the mutated item's own links. A rename also forwards the old
    # name's links to the new item, so executions still attached to it keep resolving.
    links: Mapping[str, str] | None = None


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


def validate_work_slug(raw_name: str) -> str:
    """Return *raw_name* if it is a work-item slug as written; raise otherwise."""

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


def move_work_dir(source: Path, destination: Path) -> None:
    """Move one work directory between the active and archived locations.

    A directory in both places means a copy is half-removed, half-written, or left
    by a build that has since been replaced; silently picking one loses whatever the
    other holds. Refuse with both paths named.
    """

    if destination.exists() or destination.is_symlink():
        raise ValueError(
            f"Both {source.as_posix()} and {destination.as_posix()} exist. Nothing was "
            "moved or deleted: reconcile the two directories and re-run."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)


def _publish_item_links(
    links_dir: Path,
    project_root: Path,
    paths: ProjectPaths,
    link_id: str,
    item_id: str,
) -> None:
    """Point ``work-items/<link_id>/{work,task}`` at item ``item_id``'s targets.

    The item's directory is wherever it is now (``work/`` or the archive), and a done
    item keeps its checkout (:func:`work_store.item_checkout`). An item that no longer
    exists leaves no links. The links directory is derived from the name, and a name can
    come from a session row an older build or a hand edit wrote: only a slug can name an
    item, and nothing else is joined onto a path here.
    """

    if slugify(link_id) != link_id or slugify(item_id) != item_id:
        return
    active_dir, archived_dir = locate_work_dirs(paths, item_id)
    work_dir = active_dir or archived_dir
    if work_dir is None:
        shutil.rmtree(links_dir / link_id, ignore_errors=True)
        return
    item = load_work_item_from_dir(work_dir, archived=active_dir is None)
    atomic_symlink(links_dir / link_id / "work", work_dir)
    atomic_symlink(links_dir / link_id / "task", work_store.item_checkout(item, project_root))


def write_state_locked(runtime_root: Path, work_id: str, mutation: _NamespaceMutation[T]) -> T:
    """Run one work mutation and its optional atomic state write under one lock.

    The seam deliberately disables reentrancy: a nested mutation could write an
    inner result and then let the outer mutation overwrite it from a stale
    snapshot.  Failing loudly is safer than permitting that composition.

    The item's ``work-items/<id>/{work,task}`` links are republished here, before
    the mutation (repairing whatever a crash or an older build left) and after it
    (naming where the item ended up). Every session reaches an item through these
    links, so a move needs no session write.
    """

    paths = project_paths_for_work_store(runtime_root, create_project_uuid=True)
    lock_root = _work_runtime_root(runtime_root)
    links_dir = RuntimePaths.from_root_dir(lock_root).work_items_dir
    project_root = project_root_for_work_store(runtime_root)
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
        _publish_item_links(links_dir, project_root, paths, work_id, work_id)
        result = mutation(paths, active_dir, archived_dir)
        if result.state_write is not None:
            work_dir, item = result.state_write
            _write_item(work_dir, item)
        for link_id, item_id in (result.links or {work_id: work_id}).items():
            _publish_item_links(links_dir, project_root, paths, link_id, item_id)
        return result.value


def _republish_only(
    _paths: ProjectPaths,
    _active_dir: Path | None,
    _archived_dir: Path | None,
) -> _MutationResult[None]:
    return _MutationResult(None)


def ensure_item_links(runtime_root: Path, work_id: str) -> None:
    """Publish an item's links now; drop them if the item no longer exists.

    Run before a chat links to the item, so no chat link names one that was never
    created, and after a rename to drop the old name's forwarded links.
    """

    write_state_locked(runtime_root, work_id, _republish_only)


def _item_links_dir(runtime_root: Path) -> Path:
    return RuntimePaths.from_root_dir(_work_runtime_root(runtime_root)).work_items_dir


def _link_targets(link_dir: Path) -> list[str | None]:
    """Where one ``work-items/<id>`` directory's two links point now."""

    return [
        os.readlink(link) if link.is_symlink() else None
        for link in (link_dir / "work", link_dir / "task")
    ]


def sweep_item_links(runtime_root: Path, work_ids: Collection[str]) -> int:
    """Republish the links of every item in *work_ids* and drop the rest.

    The one repair `meridian doctor` needs: any write of an item repairs its own
    links, so only an item nothing wrote since an older build moved it, and the
    links of items that are gone, wait for this. A name that is no item keeps its
    links while its ``work`` link still resolves: a rename that stopped after its
    directory moved forwards the old name to the new item, and its re-run needs that
    link. Returns how many items' links changed.
    """

    links_dir = _item_links_dir(runtime_root)
    stale = (
        sorted(
            child.name
            for child in links_dir.iterdir()
            if child.name not in work_ids and not (child / "work").is_dir()
        )
        if links_dir.is_dir()
        else []
    )
    changed = 0
    for work_id in (*sorted(work_ids), *stale):
        before = _link_targets(links_dir / work_id)
        ensure_item_links(runtime_root, work_id)
        changed += _link_targets(links_dir / work_id) != before
    return changed


def rename_stopped_after_move(runtime_root: Path, old_name: str, new_name: str) -> bool:
    """Return whether renaming *old_name* to *new_name* moved the directory and stopped.

    From the move until the rename's last step, the old name is no item and its
    ``work`` link is forwarded to the new item. That link is the only trace a stopped
    rename leaves: a name that was never renamed to *new_name* has none, so a repeated
    rename of it is a mistake, not a rename to finish. *old_name* is a validated slug.
    """

    paths = project_paths_for_work_store(runtime_root)
    if paths.work_dir is None or work_store.work_item_exists(runtime_root, old_name):
        return False
    link = _item_links_dir(runtime_root) / old_name / "work"
    new_dir = next(filter(None, locate_work_dirs(paths, new_name)), None)
    return new_dir is not None and link.is_symlink() and link.resolve() == new_dir.resolve()


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
    normalized = validate_work_slug(work_id)
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
    """Mark an item done and move its directory to the archive.

    One move, never deferred: a running spawn writes through its per-chat link, so
    the directory moving under it is transparent, and a link is what lets the move
    happen without stranding a worker on a path that no longer exists.
    """

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
        destination = archived_work_dir(paths, work_id)
        # Move first: an archived directory is done by location, so a crash before
        # the status lands leaves a readable, repairable item rather than one marked
        # done where it is still being edited.
        move_work_dir(active_dir, destination)
        return _MutationResult(archived_item, (destination, archived_item))

    return write_state_locked(runtime_root, work_id, archive)


def reopen_work_item(runtime_root: Path, work_id: str, *, status: str = "open") -> WorkItem:
    """Move a done item's directory back to work/ and reopen it.

    The one implementation behind ``work reopen``, ``work start`` on a done item and
    ``work switch`` to one. The move refuses when both locations exist, naming both,
    so neither copy is ever deleted to make room. A done status inside ``work/`` —
    nothing moved, only the status disagrees — is repaired in place, so the state
    converges instead of refusing as already active forever.
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
            repaired = reopened_item(current)
            return _MutationResult(repaired, (active_dir, repaired))

        current = load_work_item_from_dir(archived_dir, archived=True)
        reopened = reopened_item(current)
        destination = active_work_dir(paths, work_id)
        # Status first here: an interrupted reopen then leaves the item archived and
        # repair-able by re-running, never active and marked done.
        _write_item(archived_dir, reopened)
        move_work_dir(archived_dir, destination)
        return _MutationResult(reopened, (destination, reopened))

    return write_state_locked(runtime_root, work_id, reopen)


def rename_work_item(runtime_root: Path, old_work_id: str, new_name: str) -> WorkItem:
    normalized = validate_work_slug(new_name)

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
        return _MutationResult(
            load_work_item_from_dir(target, archived=active_dir is None),
            links={normalized: normalized, old_work_id: normalized},
        )

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
