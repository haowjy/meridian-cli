"""Work-item models, metadata codec, and directory location primitives.

Directory location is the sole authority for whether a work item is archived.
This neutral module is shared by the pure read store and mutation repository.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from meridian.lib.core.logging import warn_once
from meridian.lib.state.paths import (
    ProjectPaths,
    resolve_project_paths,
    resolve_project_paths_for_write,
)
from meridian.lib.utils.json_shapes import json_object, loads_json_object

MAX_SLUG_LENGTH = 64
STATUS_FILENAME = "__status.json"
WORK_STATE_VERSION = 1
SUPPORTED_WORK_STATE_VERSIONS = frozenset({WORK_STATE_VERSION})
REQUIRES_NEWER_MERIDIAN_STATUS = "requires-newer-meridian"
_NON_ALNUM_HYPHEN = re.compile(r"[^a-z0-9-]+")
_WHITESPACE_OR_UNDERSCORE = re.compile(r"[\s_]+")
_REPEATED_HYPHENS = re.compile(r"-+")
logger = structlog.get_logger(__name__)


class UnsupportedWorkStateVersion(ValueError):
    """Raised when a mutation would overwrite work metadata from a newer build."""


def _warn_newer_work_state(path: str, version: int) -> None:
    warn_once(
        ("work-state-newer", path),
        lambda: logger.warning(
            "Work item metadata was written by a newer Meridian build; "
            "upgrade Meridian to read it.",
            path=path,
            version=version,
            supported_versions=sorted(SUPPORTED_WORK_STATE_VERSIONS),
        ),
    )


def _warn_unknown_work_state_fields(path: str, fields: tuple[str, ...]) -> None:
    warn_once(
        ("work-state-unknown-fields", path),
        lambda: logger.warning(
            "Keeping unknown work item metadata fields.",
            path=path,
            fields=fields,
        ),
    )


def _warn_work_state_read_failure(path: str, error: str) -> None:
    warn_once(
        ("work-state-read-failure", path),
        lambda: logger.warning(
            "Unable to read work item metadata; using directory defaults.",
            path=path,
            error=error,
        ),
    )


def _warn_work_state_parse_failure(path: str) -> None:
    warn_once(
        ("work-state-parse-failure", path),
        lambda: logger.warning(
            "Unable to parse work item metadata; using directory defaults.",
            path=path,
        ),
    )


def _warn_work_state_validation_failure(path: str, error: str) -> None:
    warn_once(
        ("work-state-validation-failure", path),
        lambda: logger.warning(
            "Unable to validate work item metadata; using directory defaults.",
            path=path,
            error=error,
        ),
    )


def _normalize_worktree_path_text(path: str) -> str:
    """Store worktree filesystem paths with stable POSIX separators."""

    return path.replace("\\", "/")


class WorktreeMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")

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
    stored_state_extras: dict[str, object] = Field(default_factory=dict, exclude=True)
    metadata_requires_newer_meridian: bool = Field(default=False, exclude=True)

    @property
    def display_status(self) -> str:
        if self.metadata_requires_newer_meridian:
            return REQUIRES_NEWER_MERIDIAN_STATUS
        return self.status

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

    model_config = ConfigDict(frozen=True, extra="allow")

    # Readers learn the format before writers emit it. Field exclusion keeps
    # this release's files readable by strict older builds.
    v: Literal[1] = Field(default=WORK_STATE_VERSION, exclude=True)
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
    payload: dict[str, object] = dict(item.stored_state_extras)
    payload.update(
        {
            "status": item.status,
            "description": item.description,
            "goal": item.goal,
            "created_at": item.created_at,
            "archived_at": item.archived_at,
            "task_dir": item.task_dir,
            "worktree": item.worktree,
        }
    )
    return StoredWorkItemState.model_validate(payload)


def _unknown_work_state_fields(payload: dict[str, object]) -> tuple[str, ...]:
    unknown = [key for key in payload if key not in StoredWorkItemState.model_fields]
    worktree = json_object(payload.get("worktree"))
    if worktree is not None:
        unknown.extend(
            f"worktree.{key}" for key in worktree if key not in WorktreeMetadata.model_fields
        )
    return tuple(sorted(unknown))


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
    mark_unsupported: bool = False,
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
    metadata_path = status_path(work_dir)
    requires_newer_meridian = False
    try:
        raw_text = metadata_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        stored = fallback
    except OSError as exc:
        _warn_work_state_read_failure(str(metadata_path), str(exc))
        stored = fallback
    else:
        payload = loads_json_object(raw_text)
        if payload is None:
            _warn_work_state_parse_failure(str(metadata_path))
            stored = fallback
        else:
            version = payload.get("v", WORK_STATE_VERSION)
            if type(version) is int and version > max(SUPPORTED_WORK_STATE_VERSIONS):
                remedy = f"Upgrade Meridian to read work item metadata format v{version}."
                _warn_newer_work_state(str(metadata_path), version)
                if not mark_unsupported:
                    raise UnsupportedWorkStateVersion(f"{metadata_path}: {remedy}")
                requires_newer_meridian = True
                stored = fallback.model_copy(
                    update={
                        "description": remedy,
                        "goal": None,
                    }
                )
            else:
                unknown_fields = _unknown_work_state_fields(payload)
                if unknown_fields:
                    _warn_unknown_work_state_fields(str(metadata_path), unknown_fields)
                try:
                    stored = StoredWorkItemState.model_validate(payload)
                except ValidationError as exc:
                    _warn_work_state_validation_failure(str(metadata_path), str(exc))
                    stored = fallback
    # An archived directory is done by location, whatever its metadata still says:
    # the location is what `work done` and `work reopen` move, and a crash between
    # that move and the metadata write must not invent a second state.
    projected_status = "done" if archived else stored.status
    archived_at = None
    if projected_status == "done":
        archived_at = stored.archived_at or (fallback.archived_at if archived else None)
    return WorkItem(
        name=work_dir.name,
        description=stored.description,
        goal=stored.goal,
        status=projected_status,
        created_at=stored.created_at,
        archived_at=archived_at,
        task_dir=stored.task_dir,
        worktree=stored.worktree,
        stored_state_extras=dict(stored.model_extra or {}),
        metadata_requires_newer_meridian=requires_newer_meridian,
    )
