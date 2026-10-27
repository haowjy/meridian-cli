"""Per-spawn mutable scope state: the checkout a spawn pinned."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from meridian.lib.core.types import SpawnId
from meridian.lib.state.json_doc import mutate_model, read_model
from meridian.lib.state.paths import (
    resolve_project_runtime_root_for_write,
    resolve_spawn_log_dir,
    spawn_log_subpath,
)
from meridian.lib.state.spawn_aggregate import (
    PublishedArtifactUnavailable,
    published_artifact_lock,
)

_SCOPE_FILENAME = "scope.json"


class SpawnScope(BaseModel):
    """Mutable scope snapshot for one spawn: the checkout it pinned, if any."""

    model_config = ConfigDict(frozen=True, strict=True)

    task_dir: Path | None = None


class _StoredSpawnScope(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    task_dir: str | None

    @field_validator("task_dir")
    @classmethod
    def _require_nonempty_path(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("task_dir must not be empty")
        return value


def _scope_path_for_read(project_root: Path, spawn_id: str) -> Path | None:
    from meridian.lib.state.paths import resolve_project_runtime_root_or_none

    runtime_root = resolve_project_runtime_root_or_none(project_root)
    if runtime_root is None:
        return None
    return (
        resolve_spawn_log_dir(project_root, spawn_id, runtime_root=runtime_root)
        / _SCOPE_FILENAME
    )


def read_spawn_scope(project_root: Path, spawn_id: str) -> SpawnScope:
    """Read scope.json for a spawn. Tolerates missing, empty, or corrupt files."""

    path = _scope_path_for_read(project_root, spawn_id)
    if path is None:
        return SpawnScope()
    stored = read_model(path, _StoredSpawnScope)
    if stored is None:
        return SpawnScope()
    if stored.task_dir is None:
        return SpawnScope()
    return SpawnScope(task_dir=Path(stored.task_dir).expanduser().resolve())


def write_spawn_scope_task_dir(
    project_root: Path,
    spawn_id: str,
    task_dir: Path | None,
) -> bool:
    """Pin task_dir in scope.json, or unpin it with ``None``. Atomic tmp+rename."""

    runtime_root = resolve_project_runtime_root_for_write(project_root)
    path = runtime_root / spawn_log_subpath(spawn_id) / _SCOPE_FILENAME
    stored = _StoredSpawnScope(
        task_dir=task_dir.expanduser().resolve().as_posix() if task_dir is not None else None
    )
    try:
        mutate_model(
            path,
            _StoredSpawnScope,
            lambda _current: (stored, None),
            lock=published_artifact_lock(runtime_root, SpawnId(spawn_id)),
        )
    except PublishedArtifactUnavailable:
        return False
    return True


__all__ = [
    "SpawnScope",
    "read_spawn_scope",
    "write_spawn_scope_task_dir",
]
