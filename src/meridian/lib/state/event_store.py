"""Shared JSONL event store helpers for Meridian state files."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, cast

from pydantic import BaseModel, ValidationError

from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import append_durable_jsonl_line

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
    payload = event.model_dump(exclude_none=exclude_none)
    line = json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"
    with lock_file(lock_path):
        append_durable_jsonl_line(data_path, line)


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
