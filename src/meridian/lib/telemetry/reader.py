"""Local JSONL telemetry segment reader."""

from __future__ import annotations

import json
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast


def _as_string_key_dict(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    raw_dict = cast("dict[object, object]", value)
    keys = list(raw_dict)
    for key in keys:
        if not isinstance(key, str):
            return None
    return cast("dict[str, Any]", value)


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
    envelope: dict[str, Any],
    *,
    since_ts: str | None = None,
    domain: str | None = None,
    ids_filter: dict[str, str] | None = None,
) -> bool:
    if since_ts and envelope.get("ts", "") < since_ts:
        return False
    if domain and envelope.get("domain") != domain:
        return False
    if ids_filter:
        event_ids = _as_string_key_dict(envelope.get("ids"))
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
) -> Generator[dict[str, Any], None, None]:
    """Yield parsed telemetry envelopes from a segment, applying optional filters.

    Truncation-tolerant: silently skips lines that fail JSON parsing.
    """
    try:
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    loaded = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                envelope = _as_string_key_dict(loaded)
                if envelope is None:
                    continue
                if _matches_filters(
                    envelope,
                    since_ts=since_ts,
                    domain=domain,
                    ids_filter=ids_filter,
                ):
                    yield envelope
    except OSError:
        return


def snapshot_segment_offsets(telemetry_dir: Path | list[Path]) -> dict[Path, int]:
    """Return byte offsets for all known segments (tail start position).

    Captures the read cursor before following new lines. Tests and callers that
    need a deterministic starting point can pass the result as
    ``initial_offsets`` to :func:`tail_events`.
    """
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
    initial_offsets: dict[Path, int] | None = None,
) -> Generator[dict[str, Any], None, None]:
    """Follow telemetry segments, yielding new events as they arrive.

    Watches for new lines in existing segments and new segments appearing.
    Like ``tail -f`` but across rotating JSONL segment files.

    ``initial_offsets`` fixes the starting byte cursor (from
    :func:`snapshot_segment_offsets`) so callers can exclude data that already
    existed before tailing began.
    """
    dirs = telemetry_dir if isinstance(telemetry_dir, list) else [telemetry_dir]
    seen_files = (
        dict(initial_offsets)
        if initial_offsets is not None
        else snapshot_segment_offsets(dirs)
    )

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
                    with segment.open("rb") as file:
                        file.seek(offset)
                        for raw_line in file:
                            line = raw_line.decode("utf-8", errors="replace").strip()
                            if not line:
                                continue
                            try:
                                loaded = json.loads(line)
                            except (json.JSONDecodeError, ValueError):
                                continue
                            envelope = _as_string_key_dict(loaded)
                            if envelope is None:
                                continue
                            if _matches_filters(envelope, domain=domain, ids_filter=ids_filter):
                                found_new = True
                                yield envelope
                        seen_files[segment] = file.tell()
                except OSError:
                    continue
        if not found_new:
            time.sleep(poll_interval)
