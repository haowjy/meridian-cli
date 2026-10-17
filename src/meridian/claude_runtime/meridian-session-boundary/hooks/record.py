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
_SOURCES = frozenset({"startup", "resume", "fork", "clear", "compact"})


def _messaging_socket() -> str | None:
    value = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    if (
        not value
        or len(value) > 4096
        or not Path(value).is_absolute()
        or any(ord(character) < 32 for character in value)
    ):
        return None
    return value


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
    source = payload.get("source")
    if (
        payload.get("hook_event_name") != "SessionStart"
        or not isinstance(source, str) or source not in _SOURCES
    ):
        return
    session_id = payload.get("session_id")
    transcript = payload.get("transcript_path")
    if not isinstance(session_id, str) or not isinstance(transcript, str):
        return
    native = Path(transcript)
    if not native.is_absolute() or native.name != f"{session_id}.jsonl":
        return

    # A leading separator isolates a prior short/torn append from this record.
    record_payload: dict[str, object] = {
        "v": 2,
        "launch_nonce": nonce,
        # The hook command execs this interpreter; its parent is Claude.
        "pid": os.getppid(),
        "source": source,
        "session_id": session_id,
        "transcript_path": transcript,
    }
    messaging_socket = _messaging_socket()
    if messaging_socket is not None:
        record_payload["messaging_socket"] = messaging_socket
    line = b"\n" + json.dumps(
        record_payload,
        separators=(",", ":"),
    ).encode() + b"\n"
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
