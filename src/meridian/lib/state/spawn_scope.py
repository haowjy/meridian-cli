"""Per-spawn mutable scope state (task-dir overrides and tombstones)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.paths import (
    resolve_project_runtime_root_for_write,
    resolve_spawn_log_dir,
    spawn_log_subpath,
)
from meridian.lib.state.spawn_aggregate import mutate_published_spawn_artifact

logger = logging.getLogger(__name__)

_SCOPE_FILENAME = "scope.json"


@dataclass(frozen=True)
class SpawnScope:
    """Mutable scope snapshot for one spawn."""

    task_dir: Path | None = None
    task_dir_cleared: bool = False


def _scope_path_for_read(project_root: Path, spawn_id: str) -> Path:
    return resolve_spawn_log_dir(project_root, spawn_id) / _SCOPE_FILENAME


def read_spawn_scope(project_root: Path, spawn_id: str) -> SpawnScope:
    """Read scope.json for a spawn. Tolerates missing, empty, or corrupt files."""

    path = _scope_path_for_read(project_root, spawn_id)
    if not path.is_file():
        return SpawnScope()
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return SpawnScope()
        parsed: object = json.loads(text)
        if not isinstance(parsed, dict):
            return SpawnScope()
        raw = cast("dict[str, object]", parsed)
        if "task_dir" not in raw:
            return SpawnScope()
        task_dir_value = raw["task_dir"]
        if task_dir_value is None:
            return SpawnScope(task_dir=None, task_dir_cleared=True)
        if not isinstance(task_dir_value, str) or not task_dir_value.strip():
            return SpawnScope()
        return SpawnScope(
            task_dir=Path(task_dir_value).expanduser().resolve(),
            task_dir_cleared=False,
        )
    except Exception:
        logger.debug("spawn_scope: failed to read %s", path, exc_info=True)
        return SpawnScope()


def write_spawn_scope_task_dir(
    project_root: Path,
    spawn_id: str,
    task_dir: Path | None,
) -> bool:
    """Write or tombstone task_dir in scope.json. Atomic tmp+rename."""

    runtime_root = resolve_project_runtime_root_for_write(project_root)
    path = runtime_root / spawn_log_subpath(spawn_id) / _SCOPE_FILENAME
    if task_dir is None:
        payload = {"task_dir": None}
    else:
        payload = {"task_dir": task_dir.expanduser().resolve().as_posix()}
    return mutate_published_spawn_artifact(
        runtime_root,
        spawn_id,
        lambda: atomic_write_text(path, json.dumps(payload, separators=(",", ":"))),
    )


__all__ = [
    "SpawnScope",
    "read_spawn_scope",
    "write_spawn_scope_task_dir",
]
