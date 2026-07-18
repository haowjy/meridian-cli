"""Retention cleanup for local telemetry JSONL segments."""

from __future__ import annotations

import os
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from meridian.lib.telemetry.router import emit_telemetry

DEFAULT_MAX_AGE_DAYS = 7
DEFAULT_MAX_TOTAL_BYTES = 100_000_000
_DEFAULT_MAX_AGE_DAYS = DEFAULT_MAX_AGE_DAYS
_DEFAULT_MAX_TOTAL_BYTES = DEFAULT_MAX_TOTAL_BYTES


@dataclass(frozen=True)
class SegmentOwner:
    """Parsed identity from a segment filename."""

    logical_owner: str
    pid: int

    @property
    def is_cli_or_chat(self) -> bool:
        return self.logical_owner in ("cli", "chat")


@dataclass(frozen=True)
class SegmentInfo:
    path: Path
    owner: SegmentOwner | None
    size: int
    mtime: float
    live: bool

    @property
    def orphaned(self) -> bool:
        return self.owner is None


def parse_segment_owner(path: Path) -> SegmentOwner | None:
    """Parse owner from compound telemetry segment filenames.

    Compound format: <logical_owner>.<pid>-<seq>.jsonl.
    Legacy format <pid>-<seq>.jsonl is returned as None so retention treats it
    as orphaned.
    """
    if path.suffix != ".jsonl":
        return None
    stem = path.stem

    dot_idx = stem.rfind(".")
    if dot_idx > 0:
        logical_owner = stem[:dot_idx]
        instance_and_seq = stem[dot_idx + 1 :]
        parts = instance_and_seq.split("-", 1)
        if len(parts) == 2:
            try:
                pid_text, seq_text = parts
                if not pid_text.isdigit() or not seq_text.isdigit():
                    return None
                pid = int(pid_text)
                int(seq_text)
                return SegmentOwner(logical_owner=logical_owner, pid=pid)
            except ValueError:
                pass

    return None


@dataclass(frozen=True)
class RetentionStats:
    """Summary of a retention scan or cleanup pass."""

    total_segments: int
    total_bytes: int
    live_segments: int
    orphaned_segments: int
    expired_segments: int
    deleted_segments: int
    deleted_bytes: int


def scan_telemetry_segments(
    telemetry_dir: Path,
    *,
    runtime_root: Path | None = None,
    max_age_days: int = _DEFAULT_MAX_AGE_DAYS,
) -> RetentionStats:
    """Scan telemetry segments and return stats without deleting anything."""
    if not telemetry_dir.is_dir():
        return RetentionStats(0, 0, 0, 0, 0, 0, 0)
    now = time.time()
    max_age_secs = max_age_days * 24 * 60 * 60
    segments = _list_segments(telemetry_dir, runtime_root=runtime_root)
    total_bytes = sum(s.size for s in segments)
    live = sum(1 for s in segments if s.live)
    orphaned = sum(1 for s in segments if s.orphaned)
    expired = sum(1 for s in segments if not s.live and now - s.mtime > max_age_secs)
    return RetentionStats(
        total_segments=len(segments),
        total_bytes=total_bytes,
        live_segments=live,
        orphaned_segments=orphaned,
        expired_segments=expired,
        deleted_segments=0,
        deleted_bytes=0,
    )


def run_retention_cleanup(
    telemetry_dir: Path,
    *,
    runtime_root: Path | None = None,
    max_age_days: int = _DEFAULT_MAX_AGE_DAYS,
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
) -> RetentionStats:
    """Delete eligible telemetry segments by age and total-size cap."""
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    current_pid = os.getpid()
    segments = _list_segments(telemetry_dir, runtime_root=runtime_root)
    max_age_secs = max_age_days * 24 * 60 * 60

    deleted_count = 0
    deleted_bytes = 0
    initial_total = sum(s.size for s in segments)

    for segment in list(segments):
        if segment.owner is not None and segment.owner.pid == current_pid:
            continue
        if segment.live:
            continue
        if now - segment.mtime > max_age_secs and _delete_segment(segment.path):
            deleted_count += 1
            deleted_bytes += segment.size
            segments.remove(segment)

    total_size = sum(segment.size for segment in segments if segment.path.exists())
    if total_size <= max_total_bytes:
        return RetentionStats(
            total_segments=len(segments) + deleted_count,
            total_bytes=initial_total,
            live_segments=sum(1 for s in segments if s.live),
            orphaned_segments=sum(1 for s in segments if s.orphaned),
            expired_segments=0,
            deleted_segments=deleted_count,
            deleted_bytes=deleted_bytes,
        )

    # Prefer orphaned segments when enforcing the hard cap.
    for segment in sorted((s for s in segments if s.orphaned), key=lambda s: s.mtime):
        if total_size <= max_total_bytes:
            break
        if _delete_segment(segment.path):
            total_size -= segment.size
            deleted_count += 1
            deleted_bytes += segment.size

    # Last resort: closed files not owned by the current or any live process.
    for segment in sorted(
        (
            s
            for s in segments
            if (s.owner is None or s.owner.pid != current_pid) and not s.live and s.path.exists()
        ),
        key=lambda s: s.mtime,
    ):
        if total_size <= max_total_bytes:
            break
        if _delete_segment(segment.path):
            total_size -= segment.size
            deleted_count += 1
            deleted_bytes += segment.size
            emit_telemetry(
                "runtime",
                "runtime.telemetry.consumer_data_lost",
                scope="telemetry.retention",
                severity="warning",
                data={"segment": segment.path.name, "bytes_lost": segment.size},
            )

    remaining = [s for s in segments if s.path.exists()]
    return RetentionStats(
        total_segments=len(remaining) + deleted_count,
        total_bytes=initial_total,
        live_segments=sum(1 for s in remaining if s.live),
        orphaned_segments=sum(1 for s in remaining if s.orphaned),
        expired_segments=0,
        deleted_segments=deleted_count,
        deleted_bytes=deleted_bytes,
    )


def _list_segments(
    telemetry_dir: Path,
    *,
    runtime_root: Path | None = None,
) -> list[SegmentInfo]:
    # Lazy imports — telemetry initializes early; these modules depend on
    # state/core layers that aren't always available at import time.
    from meridian.lib.state.liveness import is_process_alive
    from meridian.lib.state.spawn.model import SpawnRecord

    current_pid = os.getpid()

    # Pre-load spawn records once so liveness checks are O(1) per segment
    # instead of O(all_spawns) per segment.
    spawn_records: dict[str, SpawnRecord] | None = None
    quarantined_spawn_ids: frozenset[str] = frozenset()
    spawn_state_unreadable = False
    if runtime_root is not None:
        try:
            from meridian.lib.state import spawn_store

            records = spawn_store.list_spawns(runtime_root)
            spawn_records = {r.id: r for r in records}
            quarantined_spawn_ids = frozenset(report.spawn_id for report in records.quarantines)
        except Exception:
            spawn_state_unreadable = True

    def _is_spawn_live(spawn_id: str) -> bool:
        """Check spawn liveness against pre-loaded records."""
        if spawn_state_unreadable or spawn_id in quarantined_spawn_ids:
            return True
        if spawn_records is None:
            return True
        record = spawn_records.get(spawn_id)
        if record is None:
            return False

        from meridian.lib.core.spawn_lifecycle import is_active_spawn_status

        if not is_active_spawn_status(record.status):
            return False
        if (
            record.runner_pid is not None
            and record.runner_pid > 0
            and is_process_alive(record.runner_pid)
        ):
            return True
        # Check heartbeat freshness.
        assert runtime_root is not None
        heartbeat_path = runtime_root / "spawns" / spawn_id / "heartbeat"
        try:
            mtime = heartbeat_path.stat().st_mtime
            if time.time() - mtime < 120:
                return True
        except OSError:
            pass
        return False

    segments: list[SegmentInfo] = []
    for path in telemetry_dir.glob("*.jsonl"):
        owner = parse_segment_owner(path)
        with suppress(OSError):
            stat = path.stat()
            live = False
            if owner is not None:
                if owner.pid == current_pid:
                    live = True
                elif owner.is_cli_or_chat:
                    live = is_process_alive(owner.pid)
                elif runtime_root is not None:
                    live = _is_spawn_live(owner.logical_owner)
                else:
                    live = is_process_alive(owner.pid)
            segments.append(
                SegmentInfo(
                    path=path,
                    owner=owner,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    live=live,
                )
            )
    return segments


def _delete_segment(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
