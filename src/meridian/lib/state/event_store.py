"""Shared JSONL event store helpers for Meridian state files."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, cast

from pydantic import BaseModel, ValidationError

from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import append_text_line

T = TypeVar("T")

_TAIL_SCAN_CHUNK_SIZE = 8192


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _truncate_incomplete_tail(data_path: Path) -> None:
    """Remove a final partial JSONL row left by an interrupted append."""

    try:
        with data_path.open("r+b") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            if end == 0:
                return
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) == b"\n":
                return

            cursor = end
            truncate_at = 0
            while cursor > 0:
                chunk_start = max(0, cursor - _TAIL_SCAN_CHUNK_SIZE)
                handle.seek(chunk_start)
                chunk = handle.read(cursor - chunk_start)
                newline_index = chunk.rfind(b"\n")
                if newline_index >= 0:
                    truncate_at = chunk_start + newline_index + 1
                    break
                cursor = chunk_start

            handle.truncate(truncate_at)
            handle.flush()
            os.fsync(handle.fileno())
    except FileNotFoundError:
        return


def append_event(
    data_path: Path,
    lock_path: Path,
    event: BaseModel,
    *,
    exclude_none: bool = False,
) -> None:
    payload = event.model_dump(exclude_none=exclude_none)
    line = json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"
    with lock_file(lock_path):
        _truncate_incomplete_tail(data_path)
        append_text_line(data_path, line)


def read_events(
    data_path: Path,
    parse_event: Callable[[dict[str, Any]], T | None],
) -> list[T]:
    if not data_path.exists():
        return []

    rows: list[T] = []
    with data_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                # Self-healing: ignore malformed/truncated lines.
                continue
            if not isinstance(payload, dict):
                continue
            try:
                parsed = parse_event(cast("dict[str, Any]", payload))
            except ValidationError:
                continue
            if parsed is not None:
                rows.append(parsed)
    return rows
