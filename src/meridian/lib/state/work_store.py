"""Directory-authoritative work item store.

Work items exist if and only if a work directory exists under:
- active: ``work/<work-id>/``
- archived: ``archive/work/<work-id>/``

Each work directory stores mutable metadata in ``__status.json``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path, PurePath

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from meridian.lib.state.paths import (
    ProjectPaths,
    resolve_project_paths,
    resolve_project_paths_for_write,
)

_MAX_SLUG_LENGTH = 64
_NON_ALNUM_HYPHEN = re.compile(r"[^a-z0-9-]+")
_WHITESPACE_OR_UNDERSCORE = re.compile(r"[\s_]+")
_REPEATED_HYPHENS = re.compile(r"-+")
_STATUS_FILENAME = "__status.json"
logger = structlog.get_logger(__name__)


def _normalize_worktree_path_text(path: str) -> str:
    """Store worktree filesystem paths with stable POSIX separators.

    Python's ``Path.as_posix()`` only converts separators for the host platform.
    Stored Meridian metadata must remain stable when written on Windows and read
    elsewhere, so normalize separators at the metadata boundary.
    """

    return path.replace("\\", "/")


class WorktreeMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str | None = None
    branch: str | None = None
    repo_path: str | None = None
    name: str | None = None
    pending: bool = False
    managed: bool = False

    @field_validator("path", "repo_path", mode="before")
    @classmethod
    def _normalize_path_separator(cls, value: object) -> object:
        if isinstance(value, PurePath):
            return value.as_posix()
        if isinstance(value, str):
            return _normalize_worktree_path_text(value)
        return value


class WorkItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    goal: str | None = None
    status: str
    created_at: str
    archived_at: str | None = None
    task_dir: str | None = None
    worktree: WorktreeMetadata = Field(default_factory=WorktreeMetadata)

    @property
    def worktree_path(self) -> str | None:
        return self.worktree.path

    @property
    def worktree_branch(self) -> str | None:
        return self.worktree.branch

    @property
    def worktree_repo_path(self) -> str | None:
        return self.worktree.repo_path

    @property
    def worktree_name(self) -> str | None:
        return self.worktree.name

    @property
    def worktree_pending(self) -> bool:
        return self.worktree.pending

    @property
    def worktree_managed(self) -> bool:
        return self.worktree.managed


class StoredWorkItemState(BaseModel):
    """The sole codec for the contents of ``__status.json``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    description: str
    goal: str | None
    created_at: str
    archived_at: str | None
    task_dir: str | None
    worktree: WorktreeMetadata

    @field_validator("status")
    @classmethod
    def _status_is_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Work item status must not be empty.")
        return value


def slugify(label: str) -> str:
    """Return a normalized work-item slug."""

    normalized = label.strip().lower()
    normalized = _WHITESPACE_OR_UNDERSCORE.sub("-", normalized)
    normalized = _NON_ALNUM_HYPHEN.sub("", normalized)
    normalized = _REPEATED_HYPHENS.sub("-", normalized)
    normalized = normalized.strip("-")
    normalized = normalized[:_MAX_SLUG_LENGTH].strip("-")
    return normalized


def _status_path(work_dir: Path) -> Path:
    return work_dir / _STATUS_FILENAME


def _project_paths_for_work_store(
    project_state_dir: Path, *, create_project_uuid: bool = False
) -> ProjectPaths:
    """Resolve authoritative work/archive paths for one project state dir.

    Work-store callers pass the project-owned ``.meridian`` state directory,
    not the user-home runtime root. When that directory is the canonical
    project-local ``.meridian``, honor any configured ``[context.work]`` paths.
    Synthetic test roots that are not attached to a project continue to use the
    passed directory as their literal state root.
    """

    if project_state_dir.name == ".meridian":
        resolver = resolve_project_paths_for_write if create_project_uuid else resolve_project_paths
        return resolver(project_state_dir.parent)
    return ProjectPaths.from_root_dir(project_state_dir)


def _active_dir(paths: ProjectPaths, work_id: str) -> Path:
    return paths.work_dir / work_id


def _archived_dir(paths: ProjectPaths, work_id: str) -> Path:
    return paths.work_archive_dir / work_id


def _format_ts(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace("+00:00", "Z")


def _dir_mtime_iso(work_dir: Path) -> str:
    return _format_ts(work_dir.stat().st_mtime)


def _serialize_state(state: StoredWorkItemState) -> str:
    return state.model_dump_json(indent=2) + "\n"


def _read_stored_state(path: Path) -> StoredWorkItemState | None:
    try:
        return StoredWorkItemState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return None


def _stored_state_from_item(item: WorkItem) -> StoredWorkItemState:
    return StoredWorkItemState(
        status=item.status,
        description=item.description,
        goal=item.goal,
        created_at=item.created_at,
        archived_at=item.archived_at,
        task_dir=item.task_dir,
        worktree=item.worktree,
    )


def _default_stored_state(
    work_dir: Path,
    *,
    archived: bool,
    default_status: str = "open",
    default_description: str = "",
    default_goal: str | None = None,
    default_created_at: str | None = None,
    default_archived_at: str | None = None,
    default_task_dir: str | None = None,
    default_worktree: WorktreeMetadata | None = None,
) -> StoredWorkItemState:
    created_fallback = default_created_at or _dir_mtime_iso(work_dir)
    archived_fallback = (
        default_archived_at
        if default_archived_at is not None
        else (_dir_mtime_iso(work_dir) if archived else None)
    )
    return StoredWorkItemState(
        status="done" if archived else default_status,
        description=default_description,
        goal=default_goal,
        created_at=created_fallback,
        archived_at=archived_fallback if archived else None,
        task_dir=default_task_dir,
        worktree=default_worktree or WorktreeMetadata(),
    )


def _work_item_from_dir(
    work_dir: Path,
    *,
    archived: bool,
    default_status: str = "open",
    default_description: str = "",
    default_goal: str | None = None,
    default_created_at: str | None = None,
    default_archived_at: str | None = None,
    default_task_dir: str | None = None,
    default_worktree: WorktreeMetadata | None = None,
) -> WorkItem:
    fallback = _default_stored_state(
        work_dir,
        archived=archived,
        default_status=default_status,
        default_description=default_description,
        default_goal=default_goal,
        default_created_at=default_created_at,
        default_archived_at=default_archived_at,
        default_task_dir=default_task_dir,
        default_worktree=default_worktree,
    )
    stored = _read_stored_state(_status_path(work_dir)) or fallback
    return WorkItem(
        name=work_dir.name,
        description=stored.description,
        goal=stored.goal,
        status=(
            "done" if archived else (default_status if stored.status == "done" else stored.status)
        ),
        created_at=stored.created_at,
        archived_at=(stored.archived_at or fallback.archived_at) if archived else None,
        task_dir=stored.task_dir,
        worktree=stored.worktree,
    )


def _locate_dirs(paths: ProjectPaths, work_id: str) -> tuple[Path | None, Path | None]:
    active = _active_dir(paths, work_id)
    archived = _archived_dir(paths, work_id)
    active_dir = active if active.is_dir() else None
    archived_dir = archived if archived.is_dir() else None
    return active_dir, archived_dir


def _warn_both_locations(
    work_id: str,
    active_dir: Path | None,
    archived_dir: Path | None,
) -> None:
    if active_dir is not None and archived_dir is not None:
        logger.warning(
            "Work item exists in both active and archive directories; preferring active copy.",
            work_id=work_id,
            active_dir=active_dir.as_posix(),
            archived_dir=archived_dir.as_posix(),
        )


def _is_valid_work_slug(name: str) -> bool:
    """Return True if ``name`` is a valid work-item slug (survives slugify unchanged)."""
    return bool(name) and slugify(name) == name


def _list_work_item_dirs(root_dir: Path) -> list[Path]:
    if not root_dir.is_dir():
        return []
    return [
        child
        for child in root_dir.iterdir()
        if child.is_dir() and _is_valid_work_slug(child.name)
    ]


def _has_artifacts(work_dir: Path) -> bool:
    if not work_dir.is_dir():
        return False
    return any(child.name != _STATUS_FILENAME for child in work_dir.iterdir())


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
    normalized = goal.strip()
    return normalized or None


def _normalize_worktree_path(path: str) -> str:
    try:
        return Path(path).expanduser().resolve().as_posix()
    except OSError:
        return Path(path).expanduser().as_posix()


def _normalize_task_dir_path(path: str) -> str:
    try:
        return Path(path).expanduser().resolve().as_posix()
    except OSError:
        return Path(path).expanduser().as_posix()


def create_work_item(
    runtime_root: Path,
    label: str,
    description: str = "",
    goal: str | None = None,
) -> WorkItem:
    """Create a new active work item through the locked repository."""
    from meridian.lib.state.work_repository import create_work_item as create

    return create(runtime_root, label, description, goal)



def ensure_work_item_metadata(
    runtime_root: Path,
    work_id: str,
    *,
    description: str = "",
    goal: str | None = None,
    status: str = "open",
) -> WorkItem:
    """Ensure an exact work item slug exists through the locked repository."""
    from meridian.lib.state.work_repository import ensure_work_item_metadata as ensure

    return ensure(
        runtime_root, work_id, description=description, goal=goal, status=status
    )


def heal_work_item(runtime_root: Path, work_id: str) -> WorkItem:
    """Persist the normalized projection of one existing work item."""
    from meridian.lib.state.work_repository import heal_work_item as heal

    return heal(runtime_root, work_id)


def work_item_needs_healing(runtime_root: Path, work_id: str) -> bool:
    """Return whether an existing item's persisted metadata is non-canonical."""

    paths = _project_paths_for_work_store(runtime_root)
    active_dir, archived_dir = _locate_dirs(paths, work_id)
    _warn_both_locations(work_id, active_dir, archived_dir)
    work_dir = active_dir or archived_dir
    if work_dir is None:
        return False
    item = _work_item_from_dir(work_dir, archived=active_dir is None)
    expected = _serialize_state(_stored_state_from_item(item))
    try:
        return _status_path(work_dir).read_text(encoding="utf-8") != expected
    except OSError:
        return True



def get_work_item(runtime_root: Path, work_id: str) -> WorkItem | None:
    """Load one work item from active or archived directories."""

    paths = _project_paths_for_work_store(runtime_root)
    active_dir, archived_dir = _locate_dirs(paths, work_id)
    _warn_both_locations(work_id, active_dir, archived_dir)
    if active_dir is not None:
        return _work_item_from_dir(active_dir, archived=False)
    if archived_dir is not None:
        return _work_item_from_dir(archived_dir, archived=True)
    return None


def get_active_work_item(
    runtime_root: Path,
    work_id: str,
) -> WorkItem | None:
    """Load one active work item."""

    paths = _project_paths_for_work_store(runtime_root)
    active_dir = _active_dir(paths, work_id)
    if not active_dir.is_dir():
        return None
    return _work_item_from_dir(active_dir, archived=False)


def work_scratch_dir(runtime_root: Path, work_id: str) -> Path:
    """Return current active/archive work directory if present, otherwise active path."""

    paths = _project_paths_for_work_store(runtime_root)
    active_dir, archived_dir = _locate_dirs(paths, work_id)
    _warn_both_locations(work_id, active_dir, archived_dir)
    if active_dir is not None:
        return active_dir
    if archived_dir is not None:
        return archived_dir
    return _active_dir(paths, work_id)


def list_work_items(runtime_root: Path) -> tuple[list[WorkItem], list[str]]:
    """Return active work items sorted by (created_at, name) and any warnings.

    Items that exist in both active and archive directories are included from
    the active directory with a warning rather than raising.
    """

    paths = _project_paths_for_work_store(runtime_root)
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
                f"Remove one to resolve: active={child}, archive={_archived_dir(paths, child.name)}"
            )
        items.append(_work_item_from_dir(child, archived=False))
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

    paths = _project_paths_for_work_store(runtime_root)
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
                f"Remove one to resolve: active={_active_dir(paths, child.name)}, archive={child}"
            )
            continue
        items.append(_work_item_from_dir(child, archived=True))

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


def update_work_item(
    runtime_root: Path,
    work_id: str,
    *,
    status: str | None = None,
    description: str | None = None,
    goal: str | None = None,
) -> WorkItem:
    """Update active work item metadata through the locked repository."""
    from meridian.lib.state.work_repository import update_work_item as update

    return update(
        runtime_root, work_id, status=status, description=description, goal=goal
    )



def update_work_item_task_dir(
    runtime_root: Path,
    work_id: str,
    *,
    task_dir: str | None,
) -> WorkItem:
    """Update task-dir metadata through the locked repository."""
    from meridian.lib.state.work_repository import update_work_item_task_dir as update

    return update(runtime_root, work_id, task_dir=task_dir)





def archive_work_item(
    runtime_root: Path,
    work_id: str,
    *,
    description: str | None = None,
) -> WorkItem:
    """Archive a work item through the locked repository."""
    from meridian.lib.state.work_repository import archive_work_item as archive

    return archive(runtime_root, work_id, description=description)



def reopen_work_item(
    runtime_root: Path, work_id: str, *, status: str = "open"
) -> WorkItem:
    """Reopen a work item through the locked repository."""
    from meridian.lib.state.work_repository import reopen_work_item as reopen

    return reopen(runtime_root, work_id, status=status)



def rename_work_item(runtime_root: Path, old_work_id: str, new_name: str) -> WorkItem:
    """Rename a work item through the locked repository."""
    from meridian.lib.state.work_repository import rename_work_item as rename

    return rename(runtime_root, old_work_id, new_name)



def delete_work_item(
    runtime_root: Path,
    work_id: str,
    *,
    force: bool = False,
) -> tuple[WorkItem, bool]:
    """Delete a work item through the locked repository."""
    from meridian.lib.state.work_repository import delete_work_item as delete

    return delete(runtime_root, work_id, force=force)
