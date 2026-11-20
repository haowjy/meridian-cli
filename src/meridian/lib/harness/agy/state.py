"""Paths and per-process records for the ``agy`` hook and MCP server."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, cast

from meridian.lib.platform.atomic import PRIVATE_FILE_MODE, atomic_write_text
from meridian.lib.state.user_paths import get_user_home

CONVERSATION_ID_KEY = "antigravity.google/conversation_id"
ROOT_CONVERSATION_ID_KEY = "antigravity.google/root_conversation_id"
PARENT_CONVERSATION_ID_KEY = "antigravity.google/parent_conversation_id"


def agy_root() -> Path:
    return get_user_home() / "agy"


def process_birth(pid: int) -> str | None:
    """Token that tells this ``agy`` process from a later one that reuses its PID.

    ``guard.sh`` derives the same token: field 22 of ``/proc/<pid>/stat`` on Linux, the
    ``ps`` start time with non-alphanumerics as ``_`` elsewhere.
    """

    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        stat = ""
    if stat:
        # ``comm`` may hold spaces and parentheses, so split after the last ")".
        return stat.rpartition(") ")[2].split()[19]
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return re.sub(r"[^A-Za-z0-9]", "_", out) or None


def process_dir(pid: int) -> Path | None:
    birth = process_birth(pid)
    return None if birth is None else agy_root() / f"{pid}-{birth}"


def write_private_json(path: Path, doc: dict[str, Any]) -> None:
    atomic_write_text(
        path, json.dumps(doc, separators=(",", ":")), durable=False, permissions=PRIVATE_FILE_MODE
    )


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        doc: object = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return cast("dict[str, Any]", doc) if isinstance(doc, dict) else None


def request_path(proc_dir: Path, conversation_id: str) -> Path:
    return proc_dir / "requests" / f"{conversation_id}.json"


def record_call(proc_dir: Path, meta: dict[str, Any], now: float) -> str | None:
    """Record that a tool call arrived for a root conversation; return that root ID."""

    conversation = meta.get(CONVERSATION_ID_KEY)
    root = meta.get(ROOT_CONVERSATION_ID_KEY) or conversation
    if not isinstance(root, str) or not _is_safe_id(root):
        return None
    write_private_json(
        proc_dir / "calls" / f"{root}.json",
        {
            "root_conversation_id": root,
            "conversation_id": conversation,
            "parent_conversation_id": meta.get(PARENT_CONVERSATION_ID_KEY),
            "last_call_at": now,
        },
    )
    return root


def _is_safe_id(value: str) -> bool:
    """Conversation IDs become file names; reject anything that could leave the directory."""

    return bool(value) and re.fullmatch(r"[A-Za-z0-9._-]+", value) is not None and value != ".."


def is_safe_id(value: object) -> bool:
    return isinstance(value, str) and _is_safe_id(value)


__all__ = [
    "CONVERSATION_ID_KEY",
    "ROOT_CONVERSATION_ID_KEY",
    "agy_root",
    "is_safe_id",
    "process_birth",
    "process_dir",
    "read_json",
    "record_call",
    "request_path",
    "write_private_json",
]
