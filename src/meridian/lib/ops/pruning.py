"""Pruning helpers for `meridian doctor` state retention."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.platform.locking import lock_file
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import RuntimePaths

_SECONDS_PER_DAY = 24 * 60 * 60


class OrphanProjectDir(BaseModel):
    model_config = ConfigDict(frozen=True)

    uuid: str
    path: str
    size_bytes: int
    last_activity: str
    reason: str


class StaleSpawnArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_id: str
    project_uuid: str
    path: str
    size_bytes: int
    last_activity: str


def _iso_from_mtime(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=UTC).isoformat()


def _tree_activity(path: Path) -> tuple[int, float]:
    try:
        root_stat = path.stat()
    except OSError:
        return 0, 0.0

    total_size = 0
    latest_mtime = root_stat.st_mtime
    stack: list[Path] = [path]

    while stack:
        current = stack.pop()
        try:
            current_stat = current.stat()
        except OSError:
            continue

        latest_mtime = max(latest_mtime, current_stat.st_mtime)

        if current.is_symlink():
            continue
        if current.is_file():
            total_size += current_stat.st_size
            continue

        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue

                    latest_mtime = max(latest_mtime, entry_stat.st_mtime)
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total_size += entry_stat.st_size
        except OSError:
            continue

    return total_size, latest_mtime


def _is_stale(latest_mtime: float, *, retention_days: int, now: float) -> bool:
    if retention_days < 0:
        return False
    if retention_days == 0:
        return True
    cutoff = now - (retention_days * _SECONDS_PER_DAY)
    return latest_mtime < cutoff


def scan_orphan_project_dirs(
    user_home: Path,
    retention_days: int,
    now: float,
) -> list[OrphanProjectDir]:
    """Find stale project-state directories under ``~/.meridian/projects``."""

    if retention_days < 0:
        return []

    projects_root = user_home / "projects"
    if not projects_root.is_dir():
        return []

    results: list[OrphanProjectDir] = []
    for project_dir in sorted(projects_root.iterdir(), key=lambda path: path.name):
        if not project_dir.is_dir():
            continue

        size_bytes, latest_mtime = _tree_activity(project_dir)
        active_spawns = [
            spawn
            for spawn in spawn_store.list_spawns(project_dir)
            if is_active_spawn_status(spawn.status)
        ]
        if active_spawns:
            continue

        if _is_stale(latest_mtime, retention_days=retention_days, now=now):
            results.append(
                OrphanProjectDir(
                    uuid=project_dir.name,
                    path=project_dir.as_posix(),
                    size_bytes=size_bytes,
                    last_activity=_iso_from_mtime(latest_mtime),
                    reason="stale",
                )
            )

    return results


def scan_stale_spawn_artifacts(
    runtime_root: Path,
    retention_days: int,
    active_spawn_ids: set[str],
    now: float,
) -> list[StaleSpawnArtifact]:
    """Find stale per-spawn artifact directories for the current project only."""

    return _scan_stale_spawn_artifacts(
        runtime_root=runtime_root,
        retention_days=retention_days,
        active_spawn_ids=active_spawn_ids,
        now=now,
    )


def _scan_stale_spawn_artifacts(
    *,
    runtime_root: Path,
    retention_days: int,
    active_spawn_ids: set[str],
    now: float,
) -> list[StaleSpawnArtifact]:
    """Find stale per-spawn artifact directories under runtime_root/spawns."""

    if retention_days < 0:
        return []

    per_spawn_root = runtime_root / "spawns"
    if not per_spawn_root.is_dir():
        return []

    project_uuid = runtime_root.name
    results: list[StaleSpawnArtifact] = []
    for spawn_dir in sorted(per_spawn_root.iterdir(), key=lambda path: path.name):
        if not spawn_dir.is_dir():
            continue
        spawn_id = spawn_dir.name
        if not spawn_store.is_spawn_id_shape(spawn_id):
            continue
        if spawn_id in active_spawn_ids:
            continue

        size_bytes, latest_mtime = _tree_activity(spawn_dir)
        if _is_stale(latest_mtime, retention_days=retention_days, now=now):
            results.append(
                StaleSpawnArtifact(
                    spawn_id=spawn_id,
                    project_uuid=project_uuid,
                    path=spawn_dir.as_posix(),
                    size_bytes=size_bytes,
                    last_activity=_iso_from_mtime(latest_mtime),
                )
            )

    return results


def _restore_and_retry(
    func: Callable[[str], object],
    path: str,
    exc_info: BaseException,
) -> None:
    if isinstance(exc_info, FileNotFoundError):
        return

    with suppress(OSError):
        os.chmod(path, stat.S_IWRITE)

    try:
        func(path)
        return
    except OSError as error:
        raise exc_info from error


def _prune_dir(path: Path) -> bool:
    if not path.exists():
        return False

    try:
        shutil.rmtree(path, onexc=_restore_and_retry)
    except OSError:
        return False
    return True


def prune_orphan_project_dirs(orphans: list[OrphanProjectDir]) -> int:
    """Delete stale project-state directories. Returns the number removed."""

    removed = 0
    for orphan in orphans:
        runtime_root = Path(orphan.path)
        paths = RuntimePaths.from_root_dir(runtime_root)
        with lock_file(paths.spawns_flock):
            if any(
                is_active_spawn_status(spawn.status)
                for spawn in spawn_store.list_spawns(runtime_root)
            ):
                continue
            if _prune_dir(runtime_root):
                removed += 1
    return removed


def prune_stale_spawn_artifacts(stale: list[StaleSpawnArtifact]) -> int:
    """Delete stale spawn artifact directories. Returns the number removed."""

    removed = 0
    for artifact in stale:
        artifact_path = Path(artifact.path)
        if (
            not spawn_store.is_spawn_id_shape(artifact.spawn_id)
            or artifact_path.name != artifact.spawn_id
            or artifact_path.parent.name != "spawns"
        ):
            continue
        runtime_root = artifact_path.parent.parent
        paths = RuntimePaths.from_root_dir(runtime_root)
        with lock_file(paths.spawns_flock):
            record = spawn_store.get_spawn(runtime_root, artifact.spawn_id)
            if record is not None and is_active_spawn_status(record.status):
                continue
            if _prune_dir(artifact_path):
                removed += 1
    return removed


__all__ = [
    "OrphanProjectDir",
    "StaleSpawnArtifact",
    "prune_orphan_project_dirs",
    "prune_stale_spawn_artifacts",
    "scan_orphan_project_dirs",
    "scan_stale_spawn_artifacts",
]
