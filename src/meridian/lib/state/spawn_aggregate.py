"""Cross-leaf spawn mutations that compose repository and projection persistence."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from meridian.lib.core.types import SpawnId
from meridian.lib.state.event_store import lock_file
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.process_scope_projection import scope_projection_lock_path
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import (
    is_safe_spawn_dir_name,
    read_state,
    spawn_lock_path,
)

type SpawnDeletionPrecondition = Callable[[SpawnRecord | None], bool]


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


def delete_published_spawn(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    can_delete: SpawnDeletionPrecondition,
) -> bool:
    """Delete one published spawn when its locked aggregate permits it.

    This aggregate seam composes the spawn-state and process-scope persistence
    leaves. Every published-row deletion routes through it. A cleanup claim
    prevents deletion because it is durable at-least-once intent: the reaper
    must finish or clear the claim before artifact retention may remove it.
    Callers that also need ``spawns_flock`` must acquire it first.
    """

    paths = RuntimePaths.from_root_dir(runtime_root)
    resolved_spawn_id = str(spawn_id)
    if not is_safe_spawn_dir_name(resolved_spawn_id):
        raise ValueError(f"Invalid spawn ID: {resolved_spawn_id}")
    spawn_dir = paths.spawns_dir / resolved_spawn_id

    # Global order: spawn state, then process-scope projection.
    with (
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
        try:
            shutil.rmtree(spawn_dir, onexc=_restore_spawn_artifact_permissions)
        except OSError:
            return False
        return True
