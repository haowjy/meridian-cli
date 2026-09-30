"""Crash-safe rotation and persistence of bounded attempt diagnostics."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from meridian.lib.core.types import SpawnId
from meridian.lib.launch.constants import (
    OUTPUT_FILENAME,
    REPORT_FILENAME,
    RUNNER_LIFECYCLE_FILENAME,
    STDERR_FILENAME,
    TOKENS_FILENAME,
)
from meridian.lib.state.artifact_store import ArtifactStore, make_artifact_key

_ATTEMPT_STORE_ARTIFACTS = (
    OUTPUT_FILENAME,
    STDERR_FILENAME,
    TOKENS_FILENAME,
    REPORT_FILENAME,
)
_ATTEMPT_DISK_ARTIFACTS = (
    RUNNER_LIFECYCLE_FILENAME,
    STDERR_FILENAME,
    TOKENS_FILENAME,
    REPORT_FILENAME,
)


def _recover_interrupted_attempt_rotation(log_dir: Path, attempt_prefix: str) -> bool:
    """Fold or discard a leftover staging dir from a crashed preservation.

    Returns whether ``attempt_prefix/`` already exists after recovery.
    """

    staging_dir = log_dir / f"{attempt_prefix}.tmp"
    attempt_dir = log_dir / attempt_prefix
    if not staging_dir.is_dir():
        return attempt_dir.is_dir()
    if attempt_dir.exists():
        shutil.rmtree(staging_dir)
        return True
    os.replace(staging_dir, attempt_dir)
    return True


def _preserve_attempt_artifacts(
    *,
    artifacts: ArtifactStore,
    spawn_id: SpawnId,
    log_dir: Path,
    completed_attempt: int,
) -> None:
    """Atomically move completed-attempt evidence out of the live artifact names.

    Commit point is ``os.replace(staging_dir, attempt_dir)``. A leftover
    ``attempt-N.tmp/`` from a crashed run is folded into ``attempt-N/`` when that
    directory is absent; otherwise the staging dir is discarded. Artifact-store
    copies and active-key deletion happen only after the filesystem commit so
    retries never read stale attempt-scoped store keys.
    """

    from meridian.lib.platform.atomic import fsync_directory
    from meridian.lib.platform.locking import lock_file
    from meridian.lib.state.history_changes import HistoryChanges, HistorySource
    from meridian.lib.state.spawn.repository import read_state

    changes = HistoryChanges(log_dir.parent.parent)
    source = HistorySource(kind="spawn", key=str(spawn_id))
    with lock_file(changes.mutation_lock, mode="shared"), lock_file(source.lock_path(changes.root)):
        state = read_state(changes.root / "spawns", str(spawn_id), include_prompt=False)
        if state is None or state.record_mode == "historical":
            return
        changes.mark(source)
        attempt_prefix = f"attempt-{completed_attempt}"
        staging_dir = log_dir / f"{attempt_prefix}.tmp"
        attempt_dir = log_dir / attempt_prefix
        already_committed = _recover_interrupted_attempt_rotation(log_dir, attempt_prefix)

        if already_committed:
            attempt_dir.mkdir(parents=True, exist_ok=True)
            for name in _ATTEMPT_DISK_ARTIFACTS:
                target = log_dir / name
                if target.exists():
                    os.replace(target, attempt_dir / name)
        else:
            staging_dir.mkdir(parents=True, exist_ok=True)
            for name in _ATTEMPT_DISK_ARTIFACTS:
                target = log_dir / name
                if target.exists():
                    os.replace(target, staging_dir / name)
            os.replace(staging_dir, attempt_dir)

        for name in _ATTEMPT_STORE_ARTIFACTS:
            active_key = make_artifact_key(spawn_id, name)
            if artifacts.exists(active_key):
                artifacts.put(
                    make_artifact_key(spawn_id, f"{attempt_prefix}/{name}"),
                    artifacts.get(active_key),
                )

        for name in _ATTEMPT_STORE_ARTIFACTS:
            artifacts.delete(make_artifact_key(spawn_id, name))
        fsync_directory(attempt_dir)
        fsync_directory(log_dir)


def _persist_attempt_artifacts(
    *,
    artifacts: ArtifactStore,
    spawn_id: SpawnId,
    log_dir: Path,
) -> None:
    source = log_dir / STDERR_FILENAME
    if source.exists():
        artifacts.put(make_artifact_key(spawn_id, STDERR_FILENAME), source.read_bytes())

