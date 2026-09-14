"""Spawn aggregate mutations that coordinate published-row lifetime."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from meridian.lib.core.types import SpawnId
from meridian.lib.platform.atomic import fsync_directory
from meridian.lib.state.atomic import atomic_publish_dir
from meridian.lib.state.event_store import lock_file
from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.process_scope_projection import scope_projection_lock_path
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import (
    is_safe_spawn_dir_name,
    read_state,
    spawn_lock_path,
)

type SpawnDeletionPrecondition = Callable[[SpawnRecord | None], bool]


def mutate_published_spawn_artifact(
    runtime_root: Path,
    spawn_id: SpawnId,
    mutate: Callable[[], None],
    *,
    can_mutate: Callable[[SpawnRecord], bool] | None = None,
) -> bool:
    """Mutate a spawn-owned artifact without outliving its published row."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    resolved_spawn_id = str(spawn_id)
    if not is_safe_spawn_dir_name(resolved_spawn_id):
        raise ValueError(f"Invalid spawn ID: {resolved_spawn_id}")

    with (
        lock_file(HistoryChanges(runtime_root).mutation_lock, mode="shared"),
        lock_file(spawn_lock_path(paths.spawns_dir, resolved_spawn_id), reentrant=False),
    ):
        current = read_state(paths.spawns_dir, resolved_spawn_id, include_prompt=False)
        if (
            current is None
            or current.record_mode == "historical"
            or (can_mutate is not None and not can_mutate(current))
        ):
            return False
        mutate()
        return True


def _restore_spawn_artifact_permissions(
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
    except OSError as error:
        raise exc_info from error


def ensure_spawn_staging_dir(paths: RuntimePaths) -> Path:
    staging_dir = paths.spawns_dir / ".staging"
    if os.path.lexists(staging_dir) and (staging_dir.is_symlink() or not staging_dir.is_dir()):
        raise NotADirectoryError(f"Spawn staging container must be a real directory: {staging_dir}")
    staging_dir.mkdir(parents=True, exist_ok=True)
    if staging_dir.is_symlink() or not staging_dir.is_dir():
        raise NotADirectoryError(f"Spawn staging container must be a real directory: {staging_dir}")
    return staging_dir


@dataclass(frozen=True)
class RetiredSpawn:
    """Owned staging entry, returned only after durable namespace retirement."""

    path: Path


def _remove_published_spawn[T](
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    can_delete: SpawnDeletionPrecondition,
    remove: Callable[[Path], T],
) -> T | None:
    """One ownership/lock seam for ordinary deletion and verified ZIP retirement."""
    paths = RuntimePaths.from_root_dir(runtime_root)
    resolved_spawn_id = str(spawn_id)
    if not is_safe_spawn_dir_name(resolved_spawn_id):
        raise ValueError(f"Invalid spawn ID: {resolved_spawn_id}")
    spawn_dir = paths.spawns_dir / resolved_spawn_id

    # Global order: root mutation, spawn state, then process-scope projection.
    # Callers needing spawns_flock acquire it first. A durable reaper cleanup
    # claim must be completed or cleared before artifact retention can remove it.
    changes = HistoryChanges(runtime_root)
    with (
        lock_file(changes.mutation_lock, mode="shared"),
        lock_file(spawn_lock_path(paths.spawns_dir, resolved_spawn_id)),
        lock_file(scope_projection_lock_path(runtime_root, resolved_spawn_id)),
    ):
        claim_path = spawn_dir / "reaper_cleanup_claim.json"
        if claim_path.exists() or not can_delete(
            read_state(paths.spawns_dir, resolved_spawn_id, include_prompt=False)
        ):
            return None
        if not spawn_dir.exists():
            return None
        changes.mark(HistorySource(kind="spawn", key=resolved_spawn_id))
        try:
            return remove(spawn_dir)
        except OSError:
            # A rename may already have committed before synchronization failed.
            # In that case leave staging and the caller's prepared ZIP receipt
            # intact; never return a cleanup handle with uncertain durability.
            return None


def _remove_artifacts(directory: Path) -> bool:
    shutil.rmtree(directory, onexc=_restore_spawn_artifact_permissions)
    fsync_directory(directory.parent)
    return True


def delete_published_spawn(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    can_delete: SpawnDeletionPrecondition,
) -> bool:
    """Delete ordinary published artifacts under their ownership locks."""
    return bool(
        _remove_published_spawn(
            runtime_root, spawn_id, can_delete=can_delete, remove=_remove_artifacts
        )
    )


def retire_published_spawn(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    can_delete: SpawnDeletionPrecondition,
) -> RetiredSpawn | None:
    """Retire verified ZIP-backed authority; recursive cleanup belongs outside locks."""

    def retire(directory: Path) -> RetiredSpawn:
        staging = ensure_spawn_staging_dir(RuntimePaths.from_root_dir(runtime_root))
        destination = staging / f"{directory.name}-retired-{uuid4().hex}"
        atomic_publish_dir(directory, destination)
        fsync_directory(directory.parent)
        return RetiredSpawn(destination)

    return _remove_published_spawn(runtime_root, spawn_id, can_delete=can_delete, remove=retire)


def cleanup_retired_spawn(retired: RetiredSpawn) -> bool:
    """Discard verified retirement residue, also safe after startup GC removed it."""
    try:
        return _remove_artifacts(retired.path)
    except OSError:
        return False
