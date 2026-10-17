"""Decode bounded, launch-correlated Claude SessionStart journal records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from meridian.lib.core.native_identity import (
    NativeKey,
    NativeObservation,
    NativeObservationBatch,
    Operation,
)
from meridian.lib.core.types import SpawnId
from meridian.lib.state.paths import spawn_log_subpath

_READ_LIMIT = 64 * 1024
_JOURNAL_LIMIT = 1024 * 1024
_JOURNAL_FILENAME = "claude-session-boundary.jsonl"
_ENTRY_OPERATIONS: dict[str, Operation] = {"startup": "create", "resume": "resume", "fork": "fork"}


class _Record(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    v: int
    launch_nonce: str = Field(min_length=1, max_length=256)
    pid: int = Field(gt=0)
    source: Literal["startup", "resume", "fork", "clear", "compact"]
    session_id: str = Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_-]+$")
    transcript_path: str = Field(min_length=1, max_length=4096, pattern=r"^[^\x00-\x1f]+$")
    messaging_socket: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        pattern=r"^[^\x00-\x1f]+$",
    )

    @field_validator("messaging_socket")
    @classmethod
    def _messaging_socket_is_absolute(cls, value: str | None) -> str | None:
        if value is not None and not Path(value).is_absolute():
            raise ValueError("messaging socket path is not absolute")
        return value

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

    bounded = raw[:_READ_LIMIT]
    final_newline = bounded.rfind(b"\n")
    if final_newline < 0:
        return NativeObservationBatch(next_cursor=cursor)

    consumed = bounded[: final_newline + 1]
    observations: list[NativeObservation] = []
    next_cursor = cursor
    for line in consumed.splitlines(keepends=True):
        next_cursor += len(line)
        record = _parse_record(line)
        if record is None:
            continue
        try:
            if record.v == 2 and record.launch_nonce == nonce and record.pid == pid:
                observations.append(record.observation(next_cursor))
        except ValueError:
            continue
    return NativeObservationBatch(tuple(observations), cursor + len(consumed))


def latest_messaging_socket(path: Path, *, pid: int) -> Path | None:
    """Return the last valid inbox socket published by one Claude process."""

    try:
        with path.open("rb") as handle:
            raw = handle.read(_JOURNAL_LIMIT)
    except OSError:
        return None

    final_newline = raw.rfind(b"\n")
    if final_newline < 0:
        return None

    latest: Path | None = None
    for line in raw[: final_newline + 1].splitlines(keepends=True):
        record = _parse_record(line)
        if (
            record is not None
            and record.v == 2
            and record.pid == pid
            and record.messaging_socket is not None
        ):
            latest = Path(record.messaging_socket)
    return latest
