"""Directory-authoritative work item store.

Work items exist if and only if a work directory exists under:
- active: ``work/<work-id>/``
- archived: ``archive/work/<work-id>/``

Each work directory stores mutable metadata in ``__status.json``.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import structlog
from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.event_store import utc_now_iso
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
_UNSET = object()


class WorktreeMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str | None = None
    branch: str | None = None
    pending: bool = False
    managed: bool = False


class WorkItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    goal: str | None = None
    status: str
    created_at: str
    archived_at: str | None = None
    worktree: WorktreeMetadata = Field(default_factory=WorktreeMetadata)

    @property
    def worktree_path(self) -> str | None:
        return self.worktree.path

    @property
    def worktree_branch(self) -> str | None:
        return self.worktree.branch

    @property
    def worktree_pending(self) -> bool:
        return self.worktree.pending

    @property
    def worktree_managed(self) -> bool:
        return self.worktree.managed


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


def _serialize_status(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return cast("dict[str, Any]", loaded) if isinstance(loaded, dict) else None


def _worktree_payload(metadata: WorktreeMetadata | None = None) -> dict[str, Any]:
    worktree = metadata or WorktreeMetadata()
    return {
        "path": worktree.path,
        "branch": worktree.branch,
        "pending": worktree.pending,
        "managed": worktree.managed,
    }


def _coerce_worktree_metadata(
    raw: dict[str, Any],
    *,
    default: WorktreeMetadata | None = None,
) -> tuple[WorktreeMetadata, bool]:
    changed = False
    fallback = default or WorktreeMetadata()

    nested = raw.get("worktree")
    if isinstance(nested, dict):
        nested_dict = cast("dict[str, object]", nested)
        path_value = nested_dict.get("path")
        branch_value = nested_dict.get("branch")
        pending_value = nested_dict.get("pending")
        managed_value = nested_dict.get("managed")
        has_managed_key = "managed" in nested_dict
    else:
        path_value = raw.get("worktree_path")
        branch_value = raw.get("worktree_branch")
        pending_value = raw.get("worktree_pending")
        managed_value = raw.get("worktree_managed")
        has_managed_key = "worktree_managed" in raw
        legacy_keys = ("worktree_path", "worktree_branch", "worktree_pending", "worktree_managed")
        if nested is not None or any(key in raw for key in legacy_keys):
            changed = True

    path = path_value if isinstance(path_value, str) and path_value else fallback.path
    if path_value not in (None, "") and not isinstance(path_value, str):
        changed = True

    branch = branch_value if isinstance(branch_value, str) and branch_value else fallback.branch
    if branch_value not in (None, "") and not isinstance(branch_value, str):
        changed = True

    if isinstance(pending_value, bool):
        pending = pending_value
    else:
        pending = fallback.pending
        if pending_value is not None:
            changed = True

    if isinstance(managed_value, bool):
        managed = managed_value
    elif (
        not has_managed_key
        and (
            (isinstance(path_value, str) and bool(path_value))
            or (isinstance(branch_value, str) and bool(branch_value))
            or pending_value is not None
        )
    ):
        # Compatibility inference for pre-managed records:
        # when worktree metadata exists but no `managed` flag was persisted,
        # treat it as a managed/provisioned worktree.
        managed = True
        changed = True
    else:
        managed = fallback.managed
        if managed_value is not None:
            changed = True

    return WorktreeMetadata(path=path, branch=branch, pending=pending, managed=managed), changed


def _read_or_initialize_status(
    work_dir: Path,
    *,
    archived: bool,
    default_status: str = "open",
    default_description: str = "",
    default_goal: str | None = None,
    default_created_at: str | None = None,
    default_archived_at: str | None = None,
    default_worktree: WorktreeMetadata | None = None,
) -> dict[str, Any]:
    status_file = _status_path(work_dir)
    created_fallback = default_created_at or _dir_mtime_iso(work_dir)
    archived_fallback = (
        default_archived_at
        if default_archived_at is not None
        else (_dir_mtime_iso(work_dir) if archived else None)
    )
    default_payload: dict[str, Any] = {
        "status": "done" if archived else default_status,
        "description": default_description,
        "goal": default_goal,
        "created_at": created_fallback,
        "archived_at": archived_fallback if archived else None,
        "worktree": _worktree_payload(default_worktree),
    }

    raw = _read_json_object(status_file)
    if raw is None:
        atomic_write_text(status_file, _serialize_status(default_payload))
        return default_payload

    changed = False
    payload = dict(default_payload)

    status_value = raw.get("status")
    if isinstance(status_value, str) and status_value:
        payload["status"] = status_value
    else:
        changed = True

    description_value = raw.get("description")
    if isinstance(description_value, str):
        payload["description"] = description_value
    else:
        payload["description"] = ""
        changed = True

    goal_value = raw.get("goal")
    if isinstance(goal_value, str):
        normalized_goal = _normalize_goal(goal_value)
        payload["goal"] = normalized_goal
        if normalized_goal != goal_value:
            changed = True
    elif goal_value is None:
        payload["goal"] = None
    else:
        payload["goal"] = None
        changed = True

    created_value = raw.get("created_at")
    if isinstance(created_value, str) and created_value:
        payload["created_at"] = created_value
    else:
        changed = True

    archived_value = raw.get("archived_at")
    if archived:
        reopen_interrupted = archived_value is None and payload["status"] == "open"
        if isinstance(archived_value, str) and archived_value:
            payload["archived_at"] = archived_value
        elif reopen_interrupted:
            payload["archived_at"] = None
        else:
            payload["archived_at"] = _dir_mtime_iso(work_dir)
            changed = True
        if not reopen_interrupted and payload["status"] != "done":
            payload["status"] = "done"
            changed = True
    else:
        if archived_value is not None:
            changed = True
        payload["archived_at"] = None
        if payload["status"] == "done":
            payload["status"] = default_status
            changed = True

    worktree, worktree_changed = _coerce_worktree_metadata(raw, default=default_worktree)
    payload["worktree"] = _worktree_payload(worktree)
    changed = changed or worktree_changed

    if changed:
        atomic_write_text(status_file, _serialize_status(payload))
    return payload


def _work_item_from_dir(
    work_dir: Path,
    *,
    archived: bool,
    default_status: str = "open",
    default_description: str = "",
    default_goal: str | None = None,
    default_created_at: str | None = None,
    default_archived_at: str | None = None,
    default_worktree: WorktreeMetadata | None = None,
) -> WorkItem:
    payload = _read_or_initialize_status(
        work_dir,
        archived=archived,
        default_status=default_status,
        default_description=default_description,
        default_goal=default_goal,
        default_created_at=default_created_at,
        default_archived_at=default_archived_at,
        default_worktree=default_worktree,
    )
    worktree_payload = payload.get("worktree")
    worktree = (
        WorktreeMetadata.model_validate(worktree_payload)
        if isinstance(worktree_payload, dict)
        else WorktreeMetadata()
    )
    return WorkItem(
        name=work_dir.name,
        description=str(payload["description"]),
        goal=payload["goal"] if isinstance(payload["goal"], str) else None,
        status=str(payload["status"]),
        created_at=str(payload["created_at"]),
        archived_at=payload["archived_at"] if isinstance(payload["archived_at"], str) else None,
        worktree=worktree,
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


def _list_work_item_dirs(root_dir: Path) -> list[Path]:
    if not root_dir.is_dir():
        return []
    return [child for child in root_dir.iterdir() if child.is_dir()]


def _status_payload(
    *,
    status: str,
    description: str,
    goal: str | None = None,
    created_at: str,
    archived_at: str | None,
    worktree: WorktreeMetadata | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "description": description,
        "goal": goal,
        "created_at": created_at,
        "archived_at": archived_at,
        "worktree": _worktree_payload(worktree),
    }


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


def create_work_item(
    runtime_root: Path,
    label: str,
    description: str = "",
    goal: str | None = None,
) -> WorkItem:
    """Create a new active work item directory with ``__status.json`` metadata."""

    paths = _project_paths_for_work_store(runtime_root, create_project_uuid=True)
    slug = slugify(label)
    normalized_goal = _normalize_goal(goal)
    if not slug:
        raise ValueError("Work item label must contain at least one letter or number.")

    active = _active_dir(paths, slug)
    archived = _archived_dir(paths, slug)
    if active.exists() or archived.exists():
        raise ValueError(f"Work item '{slug}' already exists. Use `meridian work switch {slug}`.")

    active.mkdir(parents=True, exist_ok=False)
    created_at = utc_now_iso()
    payload = _status_payload(
        status="open",
        description=description,
        goal=normalized_goal,
        created_at=created_at,
        archived_at=None,
        worktree=WorktreeMetadata(),
    )
    atomic_write_text(_status_path(active), _serialize_status(payload))
    return WorkItem(
        name=slug,
        description=description,
        goal=normalized_goal,
        status="open",
        created_at=created_at,
        archived_at=None,
        worktree=WorktreeMetadata(),
    )


def ensure_work_item_metadata(
    runtime_root: Path,
    work_id: str,
    *,
    description: str = "",
    goal: str | None = None,
    status: str = "open",
) -> WorkItem:
    """Ensure an exact work item slug exists on disk and return its metadata."""

    normalized = _validate_exact_slug(work_id)
    normalized_goal = _normalize_goal(goal)
    if status == "done":
        raise ValueError("'done' is reserved for archived work items.")

    paths = _project_paths_for_work_store(runtime_root, create_project_uuid=True)
    with lock_file(paths.root_dir / "work-store.flock"):
        active_dir, archived_dir = _locate_dirs(paths, normalized)
        _warn_both_locations(normalized, active_dir, archived_dir)

        if active_dir is not None:
            return _work_item_from_dir(
                active_dir,
                archived=False,
                default_status=status,
                default_description=description,
                default_goal=normalized_goal,
            )
        if archived_dir is not None:
            return _work_item_from_dir(
                archived_dir,
                archived=True,
                default_description=description,
                default_goal=normalized_goal,
            )

        created_dir = _active_dir(paths, normalized)
        created_dir.mkdir(parents=True, exist_ok=True)
        return _work_item_from_dir(
            created_dir,
            archived=False,
            default_status=status,
            default_description=description,
            default_goal=normalized_goal,
        )


def get_work_item(runtime_root: Path, work_id: str) -> WorkItem | None:
    """Load one work item from active or archived directories."""

    paths = _project_paths_for_work_store(runtime_root, create_project_uuid=True)
    active_dir, archived_dir = _locate_dirs(paths, work_id)
    _warn_both_locations(work_id, active_dir, archived_dir)
    if active_dir is not None:
        return _work_item_from_dir(active_dir, archived=False)
    if archived_dir is not None:
        return _work_item_from_dir(archived_dir, archived=True)
    return None


def get_active_work_item(runtime_root: Path, work_id: str) -> WorkItem | None:
    """Load one active work item."""

    paths = _project_paths_for_work_store(runtime_root, create_project_uuid=True)
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

    paths = _project_paths_for_work_store(runtime_root, create_project_uuid=True)
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

    paths = _project_paths_for_work_store(runtime_root, create_project_uuid=True)
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
    """Update active work item metadata and rewrite ``__status.json`` atomically."""

    paths = _project_paths_for_work_store(runtime_root, create_project_uuid=True)
    active_dir, archived_dir = _locate_dirs(paths, work_id)
    _warn_both_locations(work_id, active_dir, archived_dir)
    if active_dir is None:
        if archived_dir is not None:
            raise ValueError(
                f"Work item '{work_id}' is archived and cannot be updated. Reopen it first."
            )
        raise ValueError(f"Work item '{work_id}' not found")

    current = _work_item_from_dir(active_dir, archived=False)
    normalized_goal = _normalize_goal(goal)
    next_status = current.status if status is None else status
    if next_status == "done":
        raise ValueError("'done' is reserved for archived work items.")
    next_description = current.description if description is None else description
    next_goal = current.goal if goal is None else normalized_goal
    updated = WorkItem(
        name=current.name,
        description=next_description,
        goal=next_goal,
        status=next_status,
        created_at=current.created_at,
        archived_at=None,
        worktree=current.worktree,
    )
    atomic_write_text(
        _status_path(active_dir),
        _serialize_status(
            _status_payload(
                status=updated.status,
                description=updated.description,
                goal=updated.goal,
                created_at=updated.created_at,
                archived_at=None,
                worktree=updated.worktree,
            )
        ),
    )
    return updated


def update_work_item_worktree(
    runtime_root: Path,
    work_id: str,
    *,
    path: str | None | object = _UNSET,
    branch: str | None | object = _UNSET,
    pending: bool | object = _UNSET,
    managed: bool | object = _UNSET,
) -> WorkItem:
    """Update only the nested worktree metadata for an active work item."""

    paths = _project_paths_for_work_store(runtime_root, create_project_uuid=True)
    active_dir, archived_dir = _locate_dirs(paths, work_id)
    _warn_both_locations(work_id, active_dir, archived_dir)
    if active_dir is None:
        if archived_dir is not None:
            raise ValueError(
                f"Work item '{work_id}' is archived and cannot be updated. Reopen it first."
            )
        raise ValueError(f"Work item '{work_id}' not found")

    current = _work_item_from_dir(active_dir, archived=False)
    next_path = current.worktree.path if path is _UNSET else cast("str | None", path)
    next_branch = current.worktree.branch if branch is _UNSET else cast("str | None", branch)
    next_worktree = WorktreeMetadata(
        path=next_path,
        branch=next_branch,
        pending=current.worktree.pending if pending is _UNSET else bool(pending),
        managed=current.worktree.managed if managed is _UNSET else bool(managed),
    )
    updated = WorkItem(
        name=current.name,
        description=current.description,
        goal=current.goal,
        status=current.status,
        created_at=current.created_at,
        archived_at=current.archived_at,
        worktree=next_worktree,
    )
    atomic_write_text(
        _status_path(active_dir),
        _serialize_status(
            _status_payload(
                status=updated.status,
                description=updated.description,
                goal=updated.goal,
                created_at=updated.created_at,
                archived_at=None,
                worktree=updated.worktree,
            )
        ),
    )
    return updated


def archive_work_item(
    runtime_root: Path,
    work_id: str,
    *,
    description: str | None = None,
) -> WorkItem:
    """Archive active work by moving directory first, then setting done status."""

    paths = _project_paths_for_work_store(runtime_root, create_project_uuid=True)
    active_dir, archived_dir = _locate_dirs(paths, work_id)
    if active_dir is not None and archived_dir is not None:
        _warn_both_locations(work_id, active_dir, archived_dir)
        shutil.rmtree(archived_dir)
        archived_dir = None

    if active_dir is None:
        if archived_dir is not None:
            raise ValueError(f"Work item '{work_id}' is already archived.")
        raise ValueError(f"Work item '{work_id}' not found")

    destination = _archived_dir(paths, work_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    active_dir.rename(destination)

    current = _work_item_from_dir(destination, archived=True)
    archived_at = utc_now_iso()
    archived_description = current.description if description is None else description
    archived_item = WorkItem(
        name=current.name,
        description=archived_description,
        goal=current.goal,
        status="done",
        created_at=current.created_at,
        archived_at=archived_at,
        worktree=current.worktree.model_copy(update={"pending": False}),
    )
    atomic_write_text(
        _status_path(destination),
        _serialize_status(
            _status_payload(
                status="done",
                description=archived_item.description,
                goal=archived_item.goal,
                created_at=archived_item.created_at,
                archived_at=archived_item.archived_at,
                worktree=archived_item.worktree,
            )
        ),
    )
    return archived_item


def reopen_work_item(runtime_root: Path, work_id: str, *, status: str = "open") -> WorkItem:
    """Reopen archived work by clearing archive metadata before moving to active."""

    if status == "done":
        raise ValueError("'done' is reserved for archived work items.")

    paths = _project_paths_for_work_store(runtime_root, create_project_uuid=True)
    active_dir, archived_dir = _locate_dirs(paths, work_id)
    if active_dir is not None and archived_dir is not None:
        _warn_both_locations(work_id, active_dir, archived_dir)
        shutil.rmtree(archived_dir)
        return _work_item_from_dir(active_dir, archived=False)
    if archived_dir is None:
        if active_dir is not None:
            raise ValueError(f"Work item '{work_id}' is already active.")
        raise ValueError(f"Work item '{work_id}' not found")

    current = _work_item_from_dir(archived_dir, archived=True)
    atomic_write_text(
        _status_path(archived_dir),
        _serialize_status(
            _status_payload(
                status=status,
                description=current.description,
                goal=current.goal,
                created_at=current.created_at,
                archived_at=None,
                worktree=current.worktree.model_copy(update={"pending": False}),
            )
        ),
    )

    target = _active_dir(paths, work_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    archived_dir.rename(target)
    return _work_item_from_dir(target, archived=False, default_status=status)


def rename_work_item(runtime_root: Path, old_work_id: str, new_name: str) -> WorkItem:
    """Rename active or archived work directory in one atomic directory rename."""

    paths = _project_paths_for_work_store(runtime_root, create_project_uuid=True)
    active_dir, archived_dir = _locate_dirs(paths, old_work_id)
    if active_dir is not None and archived_dir is not None:
        _warn_both_locations(old_work_id, active_dir, archived_dir)
        shutil.rmtree(archived_dir)
        archived_dir = None
    if active_dir is None and archived_dir is None:
        raise ValueError(f"Work item '{old_work_id}' not found")

    normalized = _validate_exact_slug(new_name)
    if normalized == old_work_id:
        existing = get_work_item(runtime_root, old_work_id)
        if existing is None:
            raise ValueError(f"Work item '{old_work_id}' not found")
        return existing

    target_active = _active_dir(paths, normalized)
    target_archived = _archived_dir(paths, normalized)
    if target_active.exists() or target_archived.exists():
        raise ValueError(f"Work item '{normalized}' already exists.")

    if active_dir is not None:
        source = active_dir
        target = _active_dir(paths, normalized)
        archived = False
    else:
        source = archived_dir
        target = _archived_dir(paths, normalized)
        archived = True

    if source is None:
        raise ValueError(f"Work item '{old_work_id}' not found")

    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)
    return _work_item_from_dir(target, archived=archived)


def delete_work_item(
    runtime_root: Path,
    work_id: str,
    *,
    force: bool = False,
) -> tuple[WorkItem, bool]:
    """Delete active/archive work directories.

    Returns ``(deleted_item, had_artifacts)`` where ``had_artifacts`` indicates
    files beyond ``__status.json``.
    """

    paths = _project_paths_for_work_store(runtime_root, create_project_uuid=True)
    active_dir, archived_dir = _locate_dirs(paths, work_id)
    if active_dir is None and archived_dir is None:
        raise ValueError(f"Work item '{work_id}' not found")

    primary_dir = active_dir or archived_dir
    if primary_dir is None:
        raise ValueError(f"Work item '{work_id}' not found")

    deleted_item = (
        _work_item_from_dir(primary_dir, archived=False)
        if active_dir is not None
        else _work_item_from_dir(primary_dir, archived=True)
    )

    existing_dirs = [candidate for candidate in (active_dir, archived_dir) if candidate is not None]
    had_artifacts = any(_has_artifacts(candidate) for candidate in existing_dirs)
    if had_artifacts and not force:
        raise ValueError(f"Work item '{work_id}' has artifacts. Use --force to delete.")

    for work_dir in existing_dirs:
        shutil.rmtree(work_dir)

    return deleted_item, had_artifacts
