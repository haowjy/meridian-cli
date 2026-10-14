"""Local JSONL telemetry segment reader."""

from __future__ import annotations

import time
from collections.abc import Generator
from pathlib import Path

from meridian.lib.state.jsonl import iter_jsonl
from meridian.lib.utils.json_shapes import json_object


def _envelope(payload: dict[str, object]) -> dict[str, object]:
    return payload


def discover_segments(telemetry_dir: Path) -> list[Path]:
    """Return all JSONL segments sorted by mtime ascending."""
    if not telemetry_dir.is_dir():
        return []
    entries: list[tuple[Path, float]] = []
    for path in telemetry_dir.glob("*.jsonl"):
        try:
            entries.append((path, path.stat().st_mtime))
        except OSError:
            continue
    entries.sort(key=lambda entry: entry[1])
    return [path for path, _mtime in entries]


def _matches_filters(
    envelope: dict[str, object],
    *,
    since_ts: str | None = None,
    domain: str | None = None,
    ids_filter: dict[str, str] | None = None,
) -> bool:
    if since_ts:
        timestamp = envelope.get("ts")
        if not isinstance(timestamp, str) or timestamp < since_ts:
            return False
    if domain and envelope.get("domain") != domain:
        return False
    if ids_filter:
        event_ids = json_object(envelope.get("ids"))
        if event_ids is None:
            return False
        return all(event_ids.get(key) == value for key, value in ids_filter.items())
    return True


def read_events(
    path: Path,
    *,
    since_ts: str | None = None,
    domain: str | None = None,
    ids_filter: dict[str, str] | None = None,
) -> Generator[dict[str, object], None, None]:
    """Yield parsed telemetry envelopes from a segment, applying optional filters.

    Truncation-tolerant: an incomplete tail is left for a later read.
    """
    try:
        for envelope, _offset in iter_jsonl(path, _envelope):
            if envelope is not None and _matches_filters(
                envelope,
                since_ts=since_ts,
                domain=domain,
                ids_filter=ids_filter,
            ):
                yield envelope
    except OSError:
        return


def _snapshot_segment_offsets(telemetry_dir: Path | list[Path]) -> dict[Path, int]:
    """Return byte offsets for all known segments (tail start position)."""
    dirs = telemetry_dir if isinstance(telemetry_dir, list) else [telemetry_dir]
    seen_files: dict[Path, int] = {}
    for directory in dirs:
        for segment in discover_segments(directory):
            try:
                seen_files[segment] = segment.stat().st_size
            except OSError:
                continue
    return seen_files


def tail_events(
    telemetry_dir: Path | list[Path],
    *,
    domain: str | None = None,
    ids_filter: dict[str, str] | None = None,
    poll_interval: float = 1.0,
) -> Generator[dict[str, object], None, None]:
    """Follow telemetry segments, yielding new events as they arrive.

    Watches for new lines in existing segments and new segments appearing.
    Like ``tail -f`` but across rotating JSONL segment files.

    Byte offsets are snapshotted at call time so data written before tailing
    begins is excluded even if iteration starts later.
    """
    start_offsets = _snapshot_segment_offsets(telemetry_dir)
    return _tail_events_from_offsets(
        telemetry_dir,
        start_offsets=start_offsets,
        domain=domain,
        ids_filter=ids_filter,
        poll_interval=poll_interval,
    )


def _tail_events_from_offsets(
    telemetry_dir: Path | list[Path],
    *,
    start_offsets: dict[Path, int],
    domain: str | None = None,
    ids_filter: dict[str, str] | None = None,
    poll_interval: float = 1.0,
) -> Generator[dict[str, object], None, None]:
    dirs = telemetry_dir if isinstance(telemetry_dir, list) else [telemetry_dir]
    seen_files = dict(start_offsets)

    while True:
        found_new = False
        for directory in dirs:
            for segment in discover_segments(directory):
                offset = seen_files.get(segment, 0)
                try:
                    size = segment.stat().st_size
                except OSError:
                    continue
                if size <= offset:
                    continue
                try:
                    for envelope, end_offset in iter_jsonl(segment, _envelope, offset=offset):
                        seen_files[segment] = end_offset
                        if envelope is not None and _matches_filters(
                            envelope, domain=domain, ids_filter=ids_filter
                        ):
                            found_new = True
                            yield envelope
                except OSError:
                    continue
        if not found_new:
            time.sleep(poll_interval)
