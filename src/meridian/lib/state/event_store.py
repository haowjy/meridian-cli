"""Shared JSONL event store helpers for Meridian state files."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from pydantic import BaseModel

from meridian.lib.platform.locking import lock_file
from meridian.lib.state.jsonl import append_durable_jsonl_line, iter_jsonl

T = TypeVar("T")


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def append_event(
    data_path: Path,
    lock_path: Path,
    event: BaseModel,
    *,
    exclude_none: bool = False,
) -> None:
    payload = event.model_dump(mode="json", exclude_none=exclude_none)
    line = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    with lock_file(lock_path):
        append_durable_jsonl_line(data_path, line)


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
