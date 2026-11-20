"""Paths and per-process records for the ``agy`` hook and MCP server."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from meridian.lib.platform.atomic import PRIVATE_FILE_MODE, atomic_write_text
from meridian.lib.state.user_paths import get_user_home

CONVERSATION_ID_KEY = "antigravity.google/conversation_id"
ROOT_CONVERSATION_ID_KEY = "antigravity.google/root_conversation_id"


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


def process_dirs() -> list[tuple[Path, bool]]:
    """Every ``agy`` process directory, with whether that process (birth-matched) still runs."""

    root = agy_root()
    found: list[tuple[Path, bool]] = []
    for path in sorted(root.glob("*-*")):
        pid, _, birth = path.name.partition("-")
        if path.is_dir() and pid.isdigit() and birth:
            found.append((path, process_birth(int(pid)) == birth))
    return found


def request_path(proc_dir: Path, conversation_id: str) -> Path:
    return proc_dir / "requests" / f"{conversation_id}.json"


def row_path(proc_dir: Path, conversation_id: str) -> Path:
    """The Meridian row the server holds for a conversation: ``spawn_id``, ``chat_id``, roots."""

    return proc_dir / "rows" / f"{conversation_id}.json"


# MERIDIAN_HOME relocates the state the hook already wrote; the rest is identity and
# launch state that belongs to whichever session started the server.
_KEPT_ENV = frozenset({"MERIDIAN_HOME"})


def scrubbed_env(base: Mapping[str, str]) -> dict[str, str]:
    """``base`` without Meridian identity, which an ``agy`` started in a session would leak."""

    return {
        key: value
        for key, value in base.items()
        if key in _KEPT_ENV or not key.upper().startswith(("MERIDIAN_", "_MERIDIAN_"))
    }


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
    "process_dirs",
    "read_json",
    "request_path",
    "row_path",
    "scrubbed_env",
    "write_private_json",
]
