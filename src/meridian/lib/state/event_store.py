"""Shared JSONL event store helpers for Meridian state files."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from meridian.lib.state.jsonl import iter_jsonl

T = TypeVar("T")


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_events(
    data_path: Path,
    parse_event: Callable[[dict[str, object]], T | None],
    *,
    raw_filter: Callable[[bytes], bool] | None = None,
) -> list[T]:
    return [
        row
        for row, _offset in iter_jsonl(data_path, parse_event, raw_filter=raw_filter)
        if row is not None
    ]
