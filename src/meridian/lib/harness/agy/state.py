"""Paths and per-process records for the ``agy`` hook and MCP server."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any, cast

from meridian.lib.platform import get_home_path
from meridian.lib.platform.atomic import PRIVATE_FILE_MODE, atomic_write_text
from meridian.lib.state.user_paths import get_user_home

# What the command Meridian prints sets for the session: the hook reads them from its own
# environment when it registers the conversation and the server writes them onto the row
# (``agy`` has no flag that names either).
AGENT_ENV = "_MERIDIAN_AGY_AGENT"
APPROVAL_ENV = "_MERIDIAN_AGY_APPROVAL"
PROJECT_ENV = "_MERIDIAN_AGY_PROJECT"
# The approvals the printed command can set (the non-default ``ApprovalMode`` values); the
# hook runs before every model call and imports no policy module to name them.
PRINTED_APPROVALS = frozenset({"auto", "never", "yolo"})
CONVERSATION_ID_KEY = "antigravity.google/conversation_id"
ROOT_CONVERSATION_ID_KEY = "antigravity.google/root_conversation_id"


def native_store() -> Path:
    """Where ``agy`` keeps its conversations: ``<store>/<conversation>/.system_generated/``."""

    return get_home_path() / ".gemini" / "antigravity-cli" / "brain"


def user_settings_path() -> Path:
    """``agy``'s own settings file, beside its conversation store."""

    return get_home_path() / ".gemini" / "antigravity-cli" / "settings.json"


def agy_root() -> Path:
    return get_user_home() / "agy"


def launch_script_dir() -> Path:
    """Where a printed command too long for a shell line spills (``lib/harness/printed.py``)."""

    return agy_root() / "launch"


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


def open_conversations() -> set[str]:
    """Conversations a running ``agy`` process has registered, read fresh on every call."""

    return {
        path.stem
        for proc, alive in process_dirs()
        if alive
        for path in (proc / "requests").glob("*.json")
    }


def request_path(proc_dir: Path, conversation_id: str) -> Path:
    return proc_dir / "requests" / f"{conversation_id}.json"


def row_path(proc_dir: Path, conversation_id: str) -> Path:
    """The Meridian row the server holds for a conversation: ``spawn_id``, ``chat_id``, roots."""

    return proc_dir / "rows" / f"{conversation_id}.json"


def briefed_path(proc_dir: Path, conversation_id: str) -> Path:
    """Marks that the hook has dealt with its row's facts; ``guard.sh`` reads it."""

    return proc_dir / "briefed" / conversation_id


def seen_path(proc_dir: Path, conversation_id: str) -> Path:
    """``<offset> <transcript path>``: how far the hook has read this conversation's
    transcript, which ``guard.sh`` scans from for a compaction."""

    return proc_dir / "seen" / conversation_id


def read_seen_offset(proc_dir: Path, conversation_id: str) -> int | None:
    try:
        return int(seen_path(proc_dir, conversation_id).read_text().split(" ", 1)[0])
    except (OSError, ValueError):
        return None


def write_seen(proc_dir: Path, conversation_id: str, offset: int, transcript: str | None) -> None:
    atomic_write_text(
        seen_path(proc_dir, conversation_id),
        f"{offset} {transcript or ''}",
        durable=False,
        permissions=PRIVATE_FILE_MODE,
    )


def busy_path(proc_dir: Path, conversation_id: str) -> Path:
    """Empty file: a turn of this conversation is running. ``guard.sh`` creates it on every
    ``PreInvocation``; the ``Stop`` hook removes it when the turn ends."""

    return proc_dir / "busy" / conversation_id


def _varint(blob: bytes, pos: int) -> tuple[int, int]:
    """The varint at ``pos`` and the position after it; ``IndexError`` when it is cut off."""

    value = shift = 0
    while True:
        byte = blob[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return value, pos


def _termination_reason(blob: bytes) -> int | None:
    """Field 1 of an ``executor_metadata`` row: ``agy``'s ExecutorTerminationReason."""

    pos = 0
    try:
        while pos < len(blob):
            key, pos = _varint(blob, pos)
            field, wire = key >> 3, key & 7
            if wire == 0:
                value, pos = _varint(blob, pos)
                if field == 1:
                    return value
            elif wire == 1:
                pos += 8
            elif wire == 5:
                pos += 4
            elif wire == 2:
                length, pos = _varint(blob, pos)
                pos += length
            else:
                return None
    except IndexError:
        return None
    return None


_USER_CANCELED = 2


class TurnState:
    """Whether a turn of one conversation is running: the one answer for the watcher (which
    waits out a turn) and the idle sensor (which reports its end). Each keeps its own.

    A turn runs while ``busy/<conversation>`` exists, which ``guard.sh`` touches on every
    ``PreInvocation`` and the ``Stop`` hook removes. ``agy`` fires no ``Stop`` on Esc, so
    the mark would stay; but it appends a row to the conversation database's
    ``executor_metadata`` when an execution ends, and field 1 of that row is the
    termination reason (2 is ``USER_CANCELED``, 4 a normal end). Each time the mark is
    touched this reader notes the highest row index; a later row with reason 2 means the
    turn ended. Normal ends are not read: ``Stop`` removes the mark for them, and a
    ``Stop`` continuation's first execution also ends with a row.

    Google documents none of this (the table, the protobuf, the enum): seen on ``agy``
    1.5.15. Every failure to read it, and a missing baseline, leave the turn running, so a
    working session is never taken for an idle one; the baseline is retried on each poll.
    That also means an Esc within about a second of any model invocation (the mark is
    touched on each, and a reader notes its baseline at its next poll) can be missed, and
    the session then counts as busy until the next turn ends. The one way this fails
    open: a later build that renumbers the enum so that 2 marks an execution ending
    mid-turn would read as an Esc.
    """

    def __init__(self, proc_dir: Path, conversation: str) -> None:
        self._proc_dir = proc_dir
        self._conversation = conversation
        self._touched: int | None = None
        self._baseline: int | None = None

    def running(self) -> bool:
        try:
            touched = busy_path(self._proc_dir, self._conversation).stat().st_mtime_ns
        except OSError:
            self._touched = None
            return False
        if touched != self._touched:
            self._touched = touched
            self._baseline = self._last_index()
            return True
        if self._baseline is None:
            self._baseline = self._last_index()
            return True
        rows = self._query("SELECT data FROM executor_metadata WHERE idx > ?", self._baseline)
        return rows is None or _USER_CANCELED not in (
            _termination_reason(data) for (data,) in rows if isinstance(data, bytes)
        )

    def _last_index(self) -> int | None:
        rows = self._query("SELECT COALESCE(MAX(idx), -1) FROM executor_metadata")
        return rows[0][0] if rows else None

    def _query(self, sql: str, *params: int) -> list[tuple[Any, ...]] | None:
        """The rows of a read-only query on the conversation's database, None when it fails."""

        database = native_store().parent / "conversations" / f"{self._conversation}.db"
        try:
            with closing(
                sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=0.2)
            ) as db:
                return db.execute(sql, params).fetchall()
        except (sqlite3.Error, ValueError):
            return None


def asked_path(proc_dir: Path, conversation_id: str) -> Path:
    """Empty file: this turn's ``Stop`` already asked the model to start the watcher."""

    return proc_dir / "asked" / conversation_id


def ask_backoff_path(proc_dir: Path, conversation_id: str) -> Path:
    """``<missed asks> <turn ends left to skip>``: how far the ``Stop`` hook has backed off
    asking this conversation for a watcher. Absent or unreadable means not backed off."""

    return proc_dir / "backoff" / conversation_id


def watch_lock_path(proc_dir: Path, conversation_id: str) -> Path:
    """Empty file a running watcher holds an ``flock`` on; the lock is the fact, not the file."""

    return proc_dir / "watch" / f"{conversation_id}.lock"


def wake_path(proc_dir: Path, conversation_id: str) -> Path:
    """Text the watcher prints to wake the session when it is next idle. ``guard.sh`` removes
    it on every ``PreInvocation``: any model call satisfies it."""

    return proc_dir / "wake" / conversation_id


def inbox_path(conversation_id: str) -> Path:
    return agy_root() / "inbox" / f"{conversation_id}.jsonl"


def inflight_path(conversation_id: str) -> Path:
    return agy_root() / "inflight" / f"{conversation_id}.jsonl"


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
    "AGENT_ENV",
    "APPROVAL_ENV",
    "CONVERSATION_ID_KEY",
    "PRINTED_APPROVALS",
    "PROJECT_ENV",
    "ROOT_CONVERSATION_ID_KEY",
    "TurnState",
    "agy_root",
    "ask_backoff_path",
    "asked_path",
    "briefed_path",
    "busy_path",
    "inbox_path",
    "inflight_path",
    "is_safe_id",
    "launch_script_dir",
    "native_store",
    "open_conversations",
    "process_birth",
    "process_dir",
    "process_dirs",
    "read_json",
    "read_seen_offset",
    "request_path",
    "row_path",
    "scrubbed_env",
    "seen_path",
    "wake_path",
    "watch_lock_path",
    "write_private_json",
    "write_seen",
]
