"""Lazy migration from legacy spawn JSONL rows to v2 per-spawn state files."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.launch.constants import OUTPUT_FILENAME
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.event_store import append_event, read_events
from meridian.lib.state.liveness import is_process_alive
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.spawn.legacy_events import (
    SpawnFinalizeEvent,
    parse_event,
    reduce_events,
)
from meridian.lib.state.spawn.repository import write_state

_STARTUP_GRACE_SECS = 15.0
_HEARTBEAT_WINDOW_SECS = 120.0
_DEFERRED_MIGRATION_CACHE_SECS = 30.0
_ACTIVITY_ARTIFACTS: tuple[str, ...] = (
    "heartbeat",
    OUTPUT_FILENAME,
    "stderr.log",
)
_v2_format_cache: dict[str, tuple[bool, float]] = {}


class V2FormatMarker(BaseModel):
    """Authoritative marker showing that a runtime root uses spawn state v2."""

    model_config = ConfigDict(frozen=True)

    migrated_at: str
    v: int = 2


def _marker_path(paths: RuntimePaths) -> Path:
    return paths.spawns_dir / "v2-format.json"


def _migration_lock_path(paths: RuntimePaths) -> Path:
    return paths.spawns_dir / "migration.lock"


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_marker(paths: RuntimePaths) -> None:
    paths.spawns_dir.mkdir(parents=True, exist_ok=True)
    marker = V2FormatMarker(migrated_at=_utc_now_iso())
    atomic_write_text(_marker_path(paths), marker.model_dump_json() + "\n")


def _started_at_epoch(started_at: str | None) -> float | None:
    normalized = (started_at or "").strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _has_recent_activity(paths: RuntimePaths, spawn_id: str, now: float) -> bool:
    spawn_dir = paths.spawns_dir / spawn_id
    for artifact_name in _ACTIVITY_ARTIFACTS:
        try:
            mtime = (spawn_dir / artifact_name).stat().st_mtime
        except OSError:
            continue
        if now - mtime <= _HEARTBEAT_WINDOW_SECS:
            return True
    return False


def _is_live_v1_runner(
    paths: RuntimePaths, spawn_id: str, runner_pid: int | None, started_at: str | None, now: float
) -> bool:
    started_epoch = _started_at_epoch(started_at)
    if started_epoch is not None and now - started_epoch < _STARTUP_GRACE_SECS:
        return True
    if (
        runner_pid is not None
        and runner_pid > 0
        and is_process_alive(runner_pid, created_after_epoch=started_epoch)
    ):
        return True
    return _has_recent_activity(paths, spawn_id, now)


def _reconcile_stale_v1_active(paths: RuntimePaths, spawn_id: str) -> None:
    append_event(
        paths.spawns_jsonl,
        paths.spawns_flock,
        SpawnFinalizeEvent(
            id=spawn_id,
            status="failed",
            exit_code=1,
            finished_at=_utc_now_iso(),
            error="orphan_run",
            origin="reconciler",
        ),
        exclude_none=True,
    )


def _rename_if_present(source: Path, target: Path) -> None:
    if not source.exists() or target.exists():
        return
    try:
        source.rename(target)
    except FileNotFoundError:
        return
    except FileExistsError:
        return


def _archive_legacy_files_if_present(paths: RuntimePaths) -> None:
    """Finish archival of legacy v1 files after v2 marker is durable."""

    _rename_if_present(paths.spawns_jsonl, paths.root_dir / "spawns.legacy-v1.jsonl")
    _rename_if_present(paths.spawns_flock, paths.root_dir / "spawns.legacy-v1.jsonl.flock")


def _cache_key(runtime_root: Path) -> str:
    return str(runtime_root.resolve())


def _cached_v2_format(cache_key: str, now: float) -> bool | None:
    cached = _v2_format_cache.get(cache_key)
    if cached is None:
        return None
    cached_result, cached_at = cached
    if cached_result:
        return True
    if now - cached_at < _DEFERRED_MIGRATION_CACHE_SECS:
        return False
    return None


def _cache_v2_format(cache_key: str, result: bool, now: float | None = None) -> bool:
    _v2_format_cache[cache_key] = (result, time.time() if now is None else now)
    return result


def ensure_v2_format(runtime_root: Path) -> bool:
    """Ensure the runtime root uses v2 spawn state format.

    Returns True if v2 format is active (already active or migration succeeded).
    Returns False if migration was deferred due to live v1 runners; callers should
    use the legacy JSONL path for this access.
    """

    paths = RuntimePaths.from_root_dir(runtime_root)
    now = time.time()
    cache_key = _cache_key(runtime_root)
    cached_result = _cached_v2_format(cache_key, now)
    if cached_result is True:
        return True

    marker_path = _marker_path(paths)
    if marker_path.is_file():
        _archive_legacy_files_if_present(paths)
        return _cache_v2_format(cache_key, True, now)
    if cached_result is False:
        return False
    if not paths.spawns_jsonl.exists():
        _write_marker(paths)
        return _cache_v2_format(cache_key, True, now)

    paths.spawns_dir.mkdir(parents=True, exist_ok=True)
    with lock_file(_migration_lock_path(paths)):
        if marker_path.exists():
            return _cache_v2_format(cache_key, True, now)
        if not paths.spawns_jsonl.exists():
            _write_marker(paths)
            return _cache_v2_format(cache_key, True, now)

        events = read_events(paths.spawns_jsonl, parse_event)
        records = reduce_events(events)
        reconciled = False
        for record in records.values():
            if not is_active_spawn_status(record.status):
                continue
            if _is_live_v1_runner(paths, record.id, record.runner_pid, record.started_at, now):
                return _cache_v2_format(cache_key, False, now)
            _reconcile_stale_v1_active(paths, record.id)
            reconciled = True

        if reconciled:
            events = read_events(paths.spawns_jsonl, parse_event)
            records = reduce_events(events)

        for record in records.values():
            spawn_dir = paths.spawns_dir / record.id
            spawn_dir.mkdir(parents=True, exist_ok=True)
            prompt_path = spawn_dir / "starting-prompt.md"
            if record.prompt is not None and not prompt_path.exists():
                atomic_write_text(prompt_path, record.prompt)
            write_state(paths.spawns_dir, record, revision=1, allow_terminal_overwrite=True)

        _write_marker(paths)
        _archive_legacy_files_if_present(paths)
        return _cache_v2_format(cache_key, True, now)


__all__ = ["ensure_v2_format"]
