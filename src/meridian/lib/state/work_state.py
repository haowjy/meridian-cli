"""Work-item models, metadata codec, and directory location primitives.

Directory location is the sole authority for whether a work item is archived.
This neutral module is shared by the pure read store and mutation repository.
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

MAX_SLUG_LENGTH = 64
STATUS_FILENAME = "__status.json"
_NON_ALNUM_HYPHEN = re.compile(r"[^a-z0-9-]+")
_WHITESPACE_OR_UNDERSCORE = re.compile(r"[\s_]+")
_REPEATED_HYPHENS = re.compile(r"-+")
logger = structlog.get_logger(__name__)


def _normalize_worktree_path_text(path: str) -> str:
    """Store worktree filesystem paths with stable POSIX separators."""

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
    return normalized[:MAX_SLUG_LENGTH].strip("-")


def status_path(work_dir: Path) -> Path:
    return work_dir / STATUS_FILENAME


def project_paths_for_work_store(
    project_state_dir: Path, *, create_project_uuid: bool = False
) -> ProjectPaths:
    """Resolve authoritative work/archive paths for one project state dir."""

    if project_state_dir.name == ".meridian":
        resolver = resolve_project_paths_for_write if create_project_uuid else resolve_project_paths
        return resolver(project_state_dir.parent)
    return ProjectPaths.from_root_dir(project_state_dir)


def active_work_dir(paths: ProjectPaths, work_id: str) -> Path:
    if paths.work_dir is None:
        raise ValueError("Project work path is unresolved.")
    return paths.work_dir / work_id


def archived_work_dir(paths: ProjectPaths, work_id: str) -> Path:
    if paths.work_archive_dir is None:
        raise ValueError("Project work archive path is unresolved.")
    return paths.work_archive_dir / work_id


def locate_work_dirs(paths: ProjectPaths, work_id: str) -> tuple[Path | None, Path | None]:
    active = active_work_dir(paths, work_id)
    archived = archived_work_dir(paths, work_id)
    return active if active.is_dir() else None, archived if archived.is_dir() else None


def warn_both_locations(work_id: str, active_dir: Path | None, archived_dir: Path | None) -> None:
    if active_dir is not None and archived_dir is not None:
        logger.warning(
            "Work item exists in both active and archive directories; preferring active copy.",
            work_id=work_id,
            active_dir=active_dir.as_posix(),
            archived_dir=archived_dir.as_posix(),
        )


def serialize_work_item_state(state: StoredWorkItemState) -> str:
    return state.model_dump_json(indent=2) + "\n"


def stored_state_from_item(item: WorkItem) -> StoredWorkItemState:
    return StoredWorkItemState(
        status=item.status,
        description=item.description,
        goal=item.goal,
        created_at=item.created_at,
        archived_at=item.archived_at,
        task_dir=item.task_dir,
        worktree=item.worktree,
    )


def load_work_item_from_dir(
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
    """Project a work item from its directory and optional stored metadata."""

    def dir_mtime_iso() -> str:
        timestamp = datetime.fromtimestamp(work_dir.stat().st_mtime, tz=UTC)
        return timestamp.isoformat().replace("+00:00", "Z")

    created_fallback = default_created_at or dir_mtime_iso()
    archived_fallback = (
        default_archived_at
        if default_archived_at is not None
        else (dir_mtime_iso() if archived else None)
    )
    fallback = StoredWorkItemState(
        status="done" if archived else default_status,
        description=default_description,
        goal=default_goal,
        created_at=created_fallback,
        archived_at=archived_fallback if archived else None,
        task_dir=default_task_dir,
        worktree=default_worktree or WorktreeMetadata(),
    )
    try:
        stored = StoredWorkItemState.model_validate_json(
            status_path(work_dir).read_text(encoding="utf-8")
        )
    except (OSError, ValidationError):
        stored = fallback
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
