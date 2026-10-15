"""Pruning helpers for `meridian doctor` state retention."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.config.models import ADHOC_STALE_RETENTION_DAYS
from meridian.lib.config.project_root import has_project_marker
from meridian.lib.core.refs import parse_ref, reference_sort_key, spawn_of
from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.core.types import RunId
from meridian.lib.platform.locking import lock_file, try_lock_file
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.history_changes import HistoryChanges
from meridian.lib.state.json_doc import read_model
from meridian.lib.state.lock_gc import LockGcStats, gc_orphaned_locks
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.project_identity import (
    ADHOC_PROJECT_ID_PREFIX,
    ADHOC_TOUCH_INTERVAL_SECONDS,
    AdHocOrigin,
)
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.runs import (
    group_runs,
    retention_dependencies,
    retention_run_ids,
)
from meridian.lib.state.user_paths import get_context_home

_SECONDS_PER_DAY = 24 * 60 * 60


def _is_project_dir_name(name: str) -> bool:
    return bool(name) and not name.startswith(".")


def _is_adhoc_project_dir_name(name: str) -> bool:
    return _is_project_dir_name(name) and name.startswith(ADHOC_PROJECT_ID_PREFIX)


def _is_committed_project_dir_name(name: str) -> bool:
    return _is_project_dir_name(name) and not name.startswith(ADHOC_PROJECT_ID_PREFIX)


class OrphanProjectDir(BaseModel):
    model_config = ConfigDict(frozen=True)

    uuid: str
    path: str
    size_bytes: int
    last_activity: str
    reason: str


class AdHocProjectDir(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_id: str
    path: str | None
    runtime_root: str
    context_root: str
    size_bytes: int
    last_activity: str
    reason: str
    prunable: bool


class StaleSpawnArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_id: RunId
    project_uuid: str
    path: str
    size_bytes: int
    last_activity: str
    sibling_run_ids: tuple[RunId, ...]


@dataclass(frozen=True)
class SpawnArtifactPruneResult:
    """Artifact deletions and the lock cleanup they made possible."""

    removed: int
    lock_gc: LockGcStats


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


def _is_stale(
    latest_mtime: float,
    *,
    retention_days: int,
    now: float,
    minimum_age_seconds: int = 0,
) -> bool:
    if retention_days < 0:
        return False
    if retention_days == 0 and minimum_age_seconds == 0:
        return True
    retention_seconds = retention_days * _SECONDS_PER_DAY
    return now - latest_mtime > max(retention_seconds, minimum_age_seconds)


def _timestamp(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _is_quiescent(runtime_root: Path) -> bool:
    spawn_scan = spawn_store.list_spawns(runtime_root)
    if spawn_scan.quarantines:
        return False
    if any(is_active_spawn_status(spawn.status) for spawn in spawn_scan.records):
        return False
    return not (
        session_store.list_active_sessions(runtime_root)
        or session_store.has_live_session_leases(runtime_root)
    )


def _adhoc_activity(
    runtime_root: Path,
    *,
    context_root: Path,
) -> tuple[int, float, AdHocOrigin | None]:
    size_bytes, tree_mtime = _tree_activity(runtime_root)
    _, context_tree_mtime = _tree_activity(context_root)
    origin_path = runtime_root / "origin.json"
    try:
        origin_mtime: float | None = origin_path.stat().st_mtime
    except OSError:
        origin_mtime = None
    origin = read_model(origin_path, AdHocOrigin)
    latest = max(tree_mtime, context_tree_mtime, origin_mtime or 0.0)

    spawn_scan = spawn_store.list_spawns(runtime_root)
    for spawn in spawn_scan.records:
        timestamps = [spawn.started_at]
        if spawn.terminal is not None:
            timestamps.append(spawn.terminal.finished_at)
        for value in timestamps:
            observed = _timestamp(value)
            if observed is not None:
                latest = max(latest, observed)

    for session in session_store.list_session_generations(runtime_root):
        for value in (session.started_at, session.stopped_at):
            observed = _timestamp(value)
            if observed is not None:
                latest = max(latest, observed)

    return size_bytes, latest, origin


def _adhoc_reason(origin: AdHocOrigin | None) -> tuple[str, str | None]:
    if origin is None:
        return "adhoc-path-gone", None
    source_path = Path(origin.path).expanduser()
    try:
        if not source_path.is_dir():
            return "adhoc-path-gone", source_path.as_posix()
        if has_project_marker(source_path):
            return "adhoc-shadowed", source_path.as_posix()
    except OSError:
        return "adhoc-path-gone", source_path.as_posix()
    return "adhoc-idle", source_path.as_posix()


def _scan_adhoc_project_dir(
    runtime_root: Path,
    *,
    retention_days: int,
    stale_retention_days: int,
    now: float,
) -> AdHocProjectDir | None:
    if not _is_adhoc_project_dir_name(runtime_root.name) or not runtime_root.is_dir():
        return None

    context_root = get_context_home(
        runtime_root.name,
        user_home=runtime_root.parent.parent,
    )
    size_bytes, latest_activity, origin = _adhoc_activity(
        runtime_root,
        context_root=context_root,
    )
    reason, source_path = _adhoc_reason(origin)
    threshold = (
        min(retention_days, stale_retention_days)
        if reason != "adhoc-idle"
        else retention_days
    )
    prunable = _is_quiescent(runtime_root) and _is_stale(
        latest_activity,
        retention_days=threshold,
        now=now,
        minimum_age_seconds=ADHOC_TOUCH_INTERVAL_SECONDS,
    )

    return AdHocProjectDir(
        project_id=runtime_root.name,
        path=source_path,
        runtime_root=runtime_root.as_posix(),
        context_root=context_root.as_posix(),
        size_bytes=size_bytes,
        last_activity=_iso_from_mtime(latest_activity),
        reason=reason,
        prunable=prunable,
    )


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
        if not _is_committed_project_dir_name(project_dir.name) or not project_dir.is_dir():
            continue

        size_bytes, latest_mtime = _tree_activity(project_dir)
        if not _is_quiescent(project_dir):
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


def scan_adhoc_project_dirs(
    user_home: Path,
    retention_days: int,
    now: float,
    *,
    stale_retention_days: int = ADHOC_STALE_RETENTION_DAYS,
) -> list[AdHocProjectDir]:
    """Inspect every ad-hoc root and report its reclamation eligibility."""

    projects_root = user_home / "projects"
    if not projects_root.is_dir():
        return []

    results: list[AdHocProjectDir] = []
    for runtime_root in sorted(projects_root.iterdir(), key=lambda path: path.name):
        candidate = _scan_adhoc_project_dir(
            runtime_root,
            retention_days=retention_days,
            stale_retention_days=stale_retention_days,
            now=now,
        )
        if candidate is not None:
            results.append(candidate)
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
    all_run_ids: dict[str, list[RunId]] = {}
    for spawn_dir in sorted(per_spawn_root.iterdir(), key=lambda path: path.name):
        if not spawn_dir.is_dir():
            continue
        spawn_id = spawn_dir.name
        parsed = parse_ref(spawn_id)
        if parsed.kind not in {"spawn", "run"}:
            continue
        run_id = parse_ref(spawn_id, row_key=True)
        all_run_ids.setdefault(str(parsed.spawn_id), []).append(run_id)
        if spawn_id in active_spawn_ids:
            continue

        size_bytes, latest_mtime = _tree_activity(spawn_dir)
        if _is_stale(latest_mtime, retention_days=retention_days, now=now):
            results.append(
                StaleSpawnArtifact(
                    spawn_id=run_id,
                    project_uuid=project_uuid,
                    path=spawn_dir.as_posix(),
                    size_bytes=size_bytes,
                    last_activity=_iso_from_mtime(latest_mtime),
                    sibling_run_ids=(run_id,),
                )
            )

    results = [
        artifact.model_copy(
            update={
                "sibling_run_ids": tuple(
                    sorted(
                        all_run_ids[str(spawn_of(artifact.spawn_id))],
                        key=reference_sort_key,
                    )
                )
            }
        )
        for artifact in results
    ]

    stale_ids = {str(row.spawn_id) for row in results}
    special: set[str] = set()
    for run_ids in all_run_ids.values():
        if len(run_ids) == 1:
            continue
        group = {str(run_id) for run_id in run_ids}
        # Even a quarantined latest row must not expose an older run number.
        special.add(max(run_ids, key=reference_sort_key))
        rows: list[SpawnRecord] = []
        for run_id in run_ids:
            row = _read_prune_sibling(runtime_root, run_id)
            if row is not None:
                rows.append(row)
        for runs in group_runs(rows).values():
            special.update(retention_run_ids(runs))
        if group & active_spawn_ids or not group <= stale_ids:
            stale_ids.difference_update(group & special)
    kept = [row for row in results if str(row.spawn_id) in stale_ids]
    # Delete ordinary runs, then latest-bound, and the numeric latest last.
    return sorted(
        kept,
        key=lambda row: (
            str(row.spawn_id) in special,
            reference_sort_key(str(row.spawn_id)),
        ),
    )


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
        if (
            not _is_committed_project_dir_name(orphan.uuid)
            or runtime_root.name != orphan.uuid
            or runtime_root.parent.name != "projects"
        ):
            continue
        paths = RuntimePaths.from_root_dir(runtime_root)
        with try_lock_file(paths.project_lifetime_flock, reentrant=False) as lifetime_handle:
            if lifetime_handle is None:
                continue
            if not _is_quiescent(runtime_root):
                continue
            if _prune_dir(runtime_root):
                removed += 1
    return removed


def prune_adhoc_project_dirs(
    candidates: list[AdHocProjectDir],
    *,
    retention_days: int,
    now: float,
    stale_retention_days: int = ADHOC_STALE_RETENTION_DAYS,
) -> int:
    """Delete eligible ad-hoc runtime and context roots under the lifetime gate."""

    if retention_days < 0:
        return 0
    removed = 0
    for candidate in candidates:
        runtime_root = Path(candidate.runtime_root)
        if (
            not _is_adhoc_project_dir_name(candidate.project_id)
            or runtime_root.name != candidate.project_id
            or runtime_root.parent.name != "projects"
        ):
            continue
        paths = RuntimePaths.from_root_dir(runtime_root)
        with try_lock_file(
            paths.project_lifetime_flock,
            mode="exclusive",
            reentrant=False,
        ) as lifetime_handle:
            if lifetime_handle is None:
                continue
            refreshed = _scan_adhoc_project_dir(
                runtime_root,
                retention_days=retention_days,
                stale_retention_days=stale_retention_days,
                now=now,
            )
            if refreshed is None or not refreshed.prunable:
                continue
            if not _prune_dir(runtime_root):
                continue
            context_root = Path(refreshed.context_root)
            if context_root.exists() and not _prune_dir(context_root):
                continue
            removed += 1
    return removed


def _read_prune_sibling(runtime_root: Path, run_id: RunId) -> SpawnRecord | None:
    try:
        return spawn_store.get_spawn(runtime_root, run_id)
    except ValueError:
        return None


def prune_stale_spawn_artifacts(stale: list[StaleSpawnArtifact]) -> SpawnArtifactPruneResult:
    """Delete stale spawn artifacts and collect their newly orphaned locks."""

    removed = 0
    runtime_roots: set[Path] = set()
    for artifact in stale:
        artifact_path = Path(artifact.path)
        if (
            not spawn_store.is_spawn_id_shape(artifact.spawn_id)
            or artifact_path.name != str(artifact.spawn_id)
            or artifact_path.parent.name != "spawns"
        ):
            continue
        runtime_root = artifact_path.parent.parent
        runtime_roots.add(runtime_root)
        paths = RuntimePaths.from_root_dir(runtime_root)
        with (
            lock_file(HistoryChanges(runtime_root).mutation_lock, mode="shared"),
            lock_file(paths.spawns_flock),
        ):
            run_id = parse_ref(str(artifact.spawn_id), row_key=True)
            if run_id not in artifact.sibling_run_ids:
                continue
            spawn_id = spawn_of(run_id)
            highest_seen = max(
                parse_ref(str(key)).run or 1 for key in artifact.sibling_run_ids
            )
            successor = parse_ref(f"{spawn_id}.{highest_seen + 1}", row_key=True)
            checked_ids = (*artifact.sibling_run_ids, successor)
            rows_by_id = {
                key: _read_prune_sibling(runtime_root, key) for key in checked_ids
            }
            # A reopen published after the scan, or a quarantined sibling, makes
            # the scan-time retention decision incomplete.
            if rows_by_id[successor] is not None or any(
                row is None and (paths.spawns_dir / key / "state.json").exists()
                for key, row in rows_by_id.items()
            ):
                continue
            rows = [
                row
                for key in artifact.sibling_run_ids
                if (row := rows_by_id[key]) is not None
            ]
            run_ids = {row.id for row in rows}
            dependencies = retention_dependencies(rows)
            if (
                run_id in dependencies and (
                    dependencies[run_id].intersection(run_ids)
                    or any(is_active_spawn_status(row.status) for row in rows)
                )
            ):
                continue
            legacy_artifact_dir = runtime_root / "artifacts" / str(artifact.spawn_id)
            if legacy_artifact_dir.exists() and not _prune_dir(legacy_artifact_dir):
                continue
            if spawn_store.delete_published_spawn(
                runtime_root,
                run_id,
                can_delete=lambda record: record is None
                or not is_active_spawn_status(record.status),
            ):
                removed += 1
    lock_gc = LockGcStats()
    for runtime_root in runtime_roots:
        lock_gc = lock_gc.combine(gc_orphaned_locks(runtime_root))
    return SpawnArtifactPruneResult(removed=removed, lock_gc=lock_gc)


__all__ = [
    "AdHocProjectDir",
    "OrphanProjectDir",
    "SpawnArtifactPruneResult",
    "StaleSpawnArtifact",
    "prune_adhoc_project_dirs",
    "prune_orphan_project_dirs",
    "prune_stale_spawn_artifacts",
    "scan_adhoc_project_dirs",
    "scan_orphan_project_dirs",
    "scan_stale_spawn_artifacts",
]
