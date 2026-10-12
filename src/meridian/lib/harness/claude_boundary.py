"""Decode bounded, launch-correlated Claude SessionStart journal records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from meridian.lib.core.native_identity import (
    NativeKey,
    NativeObservation,
    NativeObservationBatch,
    Operation,
)

_READ_LIMIT = 64 * 1024
_ENTRY_OPERATIONS: dict[str, Operation] = {"startup": "create", "resume": "resume", "fork": "fork"}


class _Record(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    v: int
    launch_nonce: str = Field(min_length=1, max_length=256)
    pid: int = Field(gt=0)
    source: Literal["startup", "resume", "fork", "clear", "compact"]
    session_id: str = Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_-]+$")
    transcript_path: str = Field(min_length=1, max_length=4096, pattern=r"^[^\x00-\x1f]+$")

    def observation(self, next_cursor: int) -> NativeObservation:
        path = Path(self.transcript_path)
        if not path.is_absolute() or path.name != f"{self.session_id}.jsonl":
            raise ValueError("not an exact native transcript path")
        return NativeObservation(
            key=NativeKey("claude", str(path.parent.resolve()), self.session_id),
            entry_operation=_ENTRY_OPERATIONS.get(self.source),
            next_cursor=next_cursor,
        )


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
        try:
            record = _Record.model_validate(json.loads(line))
            if record.v == 2 and record.launch_nonce == nonce and record.pid == pid:
                observations.append(record.observation(next_cursor))
        except (ValueError, ValidationError):
            continue
    return NativeObservationBatch(tuple(observations), cursor + len(consumed))
