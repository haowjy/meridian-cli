"""Append privacy-safe Claude SessionStart facts; stdlib only."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from typing import cast

_INPUT_LIMIT = 1024 * 1024
_RECORD_LIMIT = 8 * 1024
_JOURNAL_LIMIT = 1024 * 1024


def _line(record_payload: dict[str, object]) -> bytes:
    # A leading separator isolates a prior short/torn append from this record.
    return b"\n" + json.dumps(record_payload, separators=(",", ":")).encode() + b"\n"


def record() -> None:
    destination = os.environ.get("_MERIDIAN_CLAUDE_SESSION_BOUNDARY_PATH")
    nonce = os.environ.get("_MERIDIAN_CLAUDE_SESSION_BOUNDARY_NONCE")
    if not destination or not nonce:
        return
    path = Path(destination)
    if not path.is_absolute():
        return

    raw = sys.stdin.buffer.read(_INPUT_LIMIT + 1)
    if len(raw) > _INPUT_LIMIT:
        return
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        return
    payload = cast("dict[str, object]", decoded)

    # Meridian's reader validates every field: the journal is a file it can find
    # half-written anyway. Copy only these fields; the rest of the payload stays private.
    record_payload: dict[str, object] = {
        "v": 2,
        "launch_nonce": nonce,
        # The hook command execs this interpreter; its parent is Claude.
        "pid": os.getppid(),
        "source": payload.get("source"),
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path"),
    }
    messaging_socket = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    if messaging_socket:
        record_payload["messaging_socket"] = messaging_socket
    line = _line(record_payload)
    if len(line) > _RECORD_LIMIT and record_payload.pop("messaging_socket", None):
        # Only a socket value far past any OS's PATH_MAX gets here; the session's
        # identity matters more than an inbox nobody could connect to.
        line = _line(record_payload)
    if len(line) > _RECORD_LIMIT:
        return

    # O_CREAT creates only the file, never a deleted spawn parent. One O_APPEND
    # write preserves file append order without interleaving record bytes.
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        if os.fstat(fd).st_size >= _JOURNAL_LIMIT:
            return
        os.write(fd, line)
    finally:
        os.close(fd)


if __name__ == "__main__":
    with contextlib.suppress(OSError, ValueError):
        record()
