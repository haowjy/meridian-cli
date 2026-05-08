"""Lazy migration from legacy spawn JSONL rows to v2 per-spawn state files."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.event_store import read_events
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.spawn.legacy_events import parse_event, reduce_events
from meridian.lib.state.spawn.repository import write_state

_v2_format_cache: set[str] = set()


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
    # Keep spawns.jsonl.flock in place as the stable v2 lock path.
    #
    # Renaming an actively-held lock file can split exclusion across inodes:
    # one process may still hold a lock on the renamed inode while another
    # process locks a newly created spawns.jsonl.flock path.


def _cache_key(runtime_root: Path) -> str:
    return str(runtime_root.resolve())


def _cached_v2_format(cache_key: str) -> bool:
    return cache_key in _v2_format_cache


def _cache_v2_format(cache_key: str) -> bool:
    _v2_format_cache.add(cache_key)
    return True


def ensure_v2_format(runtime_root: Path) -> bool:
    """Ensure the runtime root uses v2 spawn state format.

    Returns True if v2 format is active (already active or migration succeeded).
    """

    paths = RuntimePaths.from_root_dir(runtime_root)
    cache_key = _cache_key(runtime_root)
    if _cached_v2_format(cache_key):
        return True

    marker_path = _marker_path(paths)
    if marker_path.is_file():
        _archive_legacy_files_if_present(paths)
        return _cache_v2_format(cache_key)
    if not paths.spawns_jsonl.exists():
        _write_marker(paths)
        return _cache_v2_format(cache_key)

    paths.spawns_dir.mkdir(parents=True, exist_ok=True)
    with lock_file(_migration_lock_path(paths)):
        if marker_path.exists():
            return _cache_v2_format(cache_key)
        if not paths.spawns_jsonl.exists():
            _write_marker(paths)
            return _cache_v2_format(cache_key)

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
        return _cache_v2_format(cache_key)


__all__ = ["ensure_v2_format"]
