"""Bounded, launch-correlated Pi lifecycle observations; never a journal reader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from meridian.lib.core.native_identity import NativeSessionKey, RunBoundary


class _Identity(BaseModel):
    session_id: str = Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f]+$")
    session_file: str = Field(min_length=1, max_length=4096, pattern=r"^[^\x00-\x1f]+$")

    def key(self) -> NativeSessionKey:
        path = Path(self.session_file)
        if not path.is_absolute():
            raise ValueError("relative native path")
        return NativeSessionKey(str(path.parent.resolve()), self.session_id)


class _Event(BaseModel):
    type: Literal["session_start", "session_before_switch", "session_shutdown"]
    reason: str = Field(min_length=1, max_length=64)


class _Record(BaseModel):
    model_config = ConfigDict(strict=True)
    v: int
    launch_nonce: str = Field(min_length=1, max_length=256)
    pid: int = Field(gt=0)
    revision: int = Field(gt=0, le=2**53 - 1)
    initial: _Identity | None
    current: _Identity | None
    last_event: _Event | None
    quit: _Identity | None
    invalid_reason: str | None


def read_boundary(path: Path, *, nonce: str, pid: int | None) -> RunBoundary:
    """Missing, stale, corrupt and non-final records cannot verify an exit."""
    try:
        with path.open("rb") as handle:
            data = handle.read(16 * 1024 + 1)
        if len(data) > 16 * 1024:
            return RunBoundary()
        record = _Record.model_validate(json.loads(data))
        if (record.v != 2 or record.launch_nonce != nonce or record.pid != pid
                or record.invalid_reason is not None):
            return RunBoundary()
        entry = record.initial.key() if record.initial else None
        final_quit = (
            record.last_event is not None
            and record.last_event.type == "session_shutdown"
            and record.last_event.reason == "quit"
            and record.quit is not None
        )
        return RunBoundary(
            entry_observed=entry,
            exit=record.quit.key() if final_quit and record.quit else None,
        )
    except (OSError, ValueError, ValidationError):
        return RunBoundary()
