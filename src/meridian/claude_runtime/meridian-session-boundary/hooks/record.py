"""Claude command-hook receipt writer; stdlib only to fit the shutdown hook budget."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import cast

_LIMIT = 16 * 1024
# Native hook input may include a large user prompt; the receipt never retains it.
_INPUT_LIMIT = 1024 * 1024


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
    event = payload.get("hook_event_name")
    if event not in ("SessionStart", "SessionEnd"):
        return
    session_id = payload.get("session_id")
    transcript = payload.get("transcript_path")
    if not isinstance(session_id, str) or not isinstance(transcript, str):
        return
    native = Path(transcript)
    if not native.is_absolute() or native.name != f"{session_id}.jsonl":
        return
    current = {"session_id": session_id, "transcript_path": transcript}
    # The hook command execs this interpreter, so its parent is the Claude child.
    # A nested Claude inheriting these variables must not replace its parent's receipt.
    pid = os.getppid()
    initial: object = (
        current
        if event == "SessionStart" and payload.get("source") in ("startup", "resume", "fork")
        else None
    )
    try:
        with path.open("rb") as handle:
            previous_raw = handle.read(_LIMIT + 1)
        if len(previous_raw) > _LIMIT:
            initial = None
        else:
            previous_decoded = json.loads(previous_raw)
            previous = (
                cast("dict[str, object]", previous_decoded)
                if isinstance(previous_decoded, dict) else {}
            )
            if not previous.get("launch_nonce"):
                initial = None
            elif previous.get("launch_nonce") == nonce:
                if previous.get("pid") != pid:
                    return
                initial = previous.get("initial")
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        initial = None
    receipt = {
        "v": 1,
        "launch_nonce": nonce,
        "pid": pid,
        "initial": initial,
        "current": current,
        "event": event,
        "reason": payload.get("reason") if event == "SessionEnd" else payload.get("source"),
    }
    # Never recreate a deleted spawn directory. Only the launcher owns its lifetime.
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
            temporary = handle.name
            json.dump(receipt, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    # A failed observer must not break Claude or write into the conversation.
    # Missing/invalid receipts remain unverified at Meridian's post-exit seam.
    with contextlib.suppress(OSError, ValueError):
        record()
