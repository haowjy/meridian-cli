"""Spawn aggregate mutations that coordinate published-row lifetime."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from contextlib import suppress
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


def delete_published_spawn(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    can_delete: SpawnDeletionPrecondition,
    retire: bool = False,
) -> bool:
    """Delete one published spawn when its locked aggregate permits it.

    This aggregate seam composes the spawn-state and process-scope persistence
    leaves. Every published-row deletion routes through it. A cleanup claim
    prevents deletion because it is durable at-least-once intent: the reaper
    must finish or clear the claim before artifact retention may remove it.
    Callers that also need ``spawns_flock`` must acquire it first. Verified ZIP
    retention opts into atomic retirement before recursive cleanup, so an
    interrupted removal cannot leave a partial loose record hiding its ZIP.
    """

    paths = RuntimePaths.from_root_dir(runtime_root)
    resolved_spawn_id = str(spawn_id)
    if not is_safe_spawn_dir_name(resolved_spawn_id):
        raise ValueError(f"Invalid spawn ID: {resolved_spawn_id}")
    spawn_dir = paths.spawns_dir / resolved_spawn_id

    # Global order: spawn state, then process-scope projection.
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
            return False
        if not spawn_dir.exists():
            return False
        changes.mark(HistorySource(kind="spawn", key=resolved_spawn_id))
        try:
            removal_dir = spawn_dir
            if retire:
                staging = ensure_spawn_staging_dir(paths)
                removal_dir = staging / f"{resolved_spawn_id}-retired-{uuid4().hex}"
                atomic_publish_dir(spawn_dir, removal_dir)
                fsync_directory(paths.spawns_dir)
            shutil.rmtree(removal_dir, onexc=_restore_spawn_artifact_permissions)
            fsync_directory(removal_dir.parent)
        except OSError:
            return False
        return True
