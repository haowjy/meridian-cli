"""Read bounded, launch-correlated Claude hook receipts; never discover a transcript."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from meridian.lib.core.native_identity import NativeKey, PostExit


class _Identity(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    session_id: str = Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_-]+$")
    transcript_path: str = Field(min_length=1, max_length=4096, pattern=r"^[^\x00-\x1f]+$")

    def key(self) -> NativeKey:
        path = Path(self.transcript_path)
        if not path.is_absolute() or path.name != f"{self.session_id}.jsonl":
            raise ValueError("not an exact native transcript path")
        return NativeKey("claude", str(path.parent.resolve()), self.session_id)


class _Record(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    v: int
    launch_nonce: str = Field(min_length=1, max_length=256)
    pid: int = Field(gt=0)
    initial: _Identity | None
    current: _Identity
    event: Literal["SessionStart", "SessionEnd"]
    reason: str | None


def read_boundary(path: Path, *, nonce: str, pid: int | None) -> PostExit:
    """Switches, missing startup, stale/non-final receipts cannot establish an exit.

    The Claude adapter must validate the exact exit transcript before returning
    this observation, including for an already-bound chat. Receipts alone do not
    prove persistence; the allocator may reuse an existing chat without a file check.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(16 * 1024 + 1)
        if len(raw) > 16 * 1024:
            return PostExit()
        record = _Record.model_validate(json.loads(raw))
        if (record.v != 1 or record.launch_nonce != nonce
                or record.pid != pid or record.initial is None):
            return PostExit()
        initial = record.initial.key()
        final = (
            record.current.key()
            if record.event == "SessionEnd"
            and record.reason in {"prompt_input_exit", "logout", "other"}
            else None
        )
        return PostExit(entry_observed=initial, exit=final)
    except (OSError, ValueError, ValidationError):
        return PostExit()
