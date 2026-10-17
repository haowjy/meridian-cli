"""Decode bounded, launch-correlated Claude SessionStart journal records."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from meridian.lib.core.native_identity import (
    NativeKey,
    NativeObservation,
    NativeObservationBatch,
    Operation,
)
from meridian.lib.core.types import SpawnId
from meridian.lib.state.paths import spawn_log_subpath

_READ_LIMIT = 64 * 1024
_RECORD_LIMIT = 8 * 1024
_JOURNAL_LIMIT = 1024 * 1024
_JOURNAL_READ_LIMIT = _JOURNAL_LIMIT + _RECORD_LIMIT
_JOURNAL_FILENAME = "claude-session-boundary.jsonl"
_ENTRY_OPERATIONS: dict[str, Operation] = {"startup": "create", "resume": "resume", "fork": "fork"}


class _Record(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    v: Literal[2]
    launch_nonce: str = Field(min_length=1, max_length=256)
    pid: int = Field(gt=0)
    source: Literal["startup", "resume", "fork", "clear", "compact"]
    session_id: str = Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_-]+$")
    transcript_path: str = Field(min_length=1, max_length=4096, pattern=r"^[^\x00-\x1f]+$")
    messaging_socket: str | None = None
    recorded_at: float | None = None

    def observation(self, next_cursor: int) -> NativeObservation:
        path = Path(self.transcript_path)
        if not path.is_absolute() or path.name != f"{self.session_id}.jsonl":
            raise ValueError("not an exact native transcript path")
        return NativeObservation(
            key=NativeKey("claude", str(path.parent.resolve()), self.session_id),
            entry_operation=_ENTRY_OPERATIONS.get(self.source),
            next_cursor=next_cursor,
        )


def claude_boundary_journal_path(runtime_root: Path, spawn_id: SpawnId | str) -> Path:
    """Return the launch-owned Claude SessionStart journal path."""

    return (runtime_root / spawn_log_subpath(spawn_id) / _JOURNAL_FILENAME).resolve()


def _parse_record(line: bytes) -> _Record | None:
    try:
        return _Record.model_validate(json.loads(line))
    except (ValueError, ValidationError):
        return None


def _iter_complete_records(
    raw: bytes,
    *,
    cursor: int = 0,
) -> Iterator[tuple[int, _Record | None]]:
    """Yield the ending cursor and decoded value for each complete journal line."""

    final_newline = raw.rfind(b"\n")
    if final_newline < 0:
        return

    next_cursor = cursor
    for line in raw[: final_newline + 1].splitlines(keepends=True):
        next_cursor += len(line)
        yield next_cursor, _parse_record(line)


def _usable_messaging_socket(value: str | None) -> Path | None:
    if not value or not Path(value).is_absolute():
        return None
    try:
        if len(value.encode()) > 107:
            return None
    except UnicodeEncodeError:
        return None
    if any(ord(character) < 32 for character in value):
        return None
    return Path(value)


def read_observations(
    path: Path,
    *,
    nonce: str,
    pid: int,
    cursor: int,
) -> NativeObservationBatch:
    """Read complete journal lines from *cursor*, filtering foreign ownership facts."""

    try:
        with path.open("rb") as handle:
            handle.seek(cursor)
            raw = handle.read(_READ_LIMIT + 1)
    except (OSError, ValueError):
        return NativeObservationBatch(next_cursor=cursor)
    if not raw:
        return NativeObservationBatch(next_cursor=cursor)

    observations: list[NativeObservation] = []
    next_cursor = cursor
    for next_cursor, record in _iter_complete_records(raw[:_READ_LIMIT], cursor=cursor):
        if record is None:
            continue
        try:
            if record.launch_nonce == nonce and record.pid == pid:
                observations.append(record.observation(next_cursor))
        except ValueError:
            continue
    return NativeObservationBatch(tuple(observations), next_cursor)


def latest_messaging_socket(path: Path, *, pid: int) -> tuple[Path, float | None] | None:
    """Return the last usable inbox socket published by one Claude process."""

    try:
        with path.open("rb") as handle:
            raw = handle.read(_JOURNAL_READ_LIMIT)
    except OSError:
        return None

    latest: tuple[Path, float | None] | None = None
    for _, record in _iter_complete_records(raw):
        if record is None or record.pid != pid:
            continue
        socket_path = _usable_messaging_socket(record.messaging_socket)
        if socket_path is not None:
            latest = socket_path, record.recorded_at
    return latest
