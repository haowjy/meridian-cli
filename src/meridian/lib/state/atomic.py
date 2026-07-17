"""Crash-safe file write helpers for authoritative Meridian state."""

from __future__ import annotations

import json
import os
from pathlib import Path

from meridian.lib.platform.atomic import atomic_replace, fsync_directory


def atomic_publish_dir(stage_dir: Path, dest_dir: Path) -> None:
    """Publish a complete staged directory and sync its parent entry."""

    if os.path.lexists(dest_dir):
        raise FileExistsError(f"Refusing to publish over existing destination: {dest_dir}")
    os.replace(stage_dir, dest_dir)
    fsync_directory(dest_dir.parent)


def atomic_write_text(path: Path, content: str) -> None:
    """Write text via same-directory temp file + fsync + replace."""

    with atomic_replace(path, permissions=0o600) as handle:
        handle.write(content)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes via same-directory temp file + fsync + replace."""

    with atomic_replace(path, mode="wb", encoding=None, permissions=0o600) as handle:
        handle.write(data)


def _jsonl_tail_needs_repair(path: Path) -> bool:
    """Return True when an existing JSONL file lacks a trailing newline."""

    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            if end == 0:
                return False
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) != b"\n"
    except FileNotFoundError:
        return False


def _repaired_jsonl_bytes(content: bytes) -> bytes | None:
    """Return repaired JSONL bytes, or None when no repair is needed.

    Callers write JSON object rows only (events, permission transitions,
    control actions). A complete tail must parse as a JSON object; torn tails are
    dropped to the last newline.
    """

    if not content or content.endswith(b"\n"):
        return None

    last_newline = content.rfind(b"\n")
    if last_newline < 0:
        prefix = b""
        tail = content
    else:
        prefix = content[: last_newline + 1]
        tail = content[last_newline + 1 :]

    try:
        parsed = json.loads(tail.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = None

    if isinstance(parsed, dict):
        return prefix + tail + b"\n"
    return prefix


def repair_jsonl_tail(path: Path) -> None:
    """Repair a torn or delimiter-less JSONL tail via atomic inode replacement.

    Repair is best-effort: on Windows, ``os.replace`` raises when another handle
    keeps the target open, and ``read_bytes`` can fail on sharing violations. Failing
    the caller's append because repair could not run would be strictly worse than
    the pre-repair status quo (append onto the torn tail). Skipping repair restores
    that status quo; POSIX is unaffected because replace of open files succeeds there.
    """

    try:
        if not _jsonl_tail_needs_repair(path):
            return

        content = path.read_bytes()
        repaired = _repaired_jsonl_bytes(content)
        if repaired is None:
            return

        with atomic_replace(path, mode="wb", encoding=None, permissions="preserve") as handle:
            handle.write(repaired)
    except OSError:
        return


def append_text_line(path: Path, line: str) -> None:
    """Append one line and fsync before returning.

    Opens in binary mode so ``\\n`` is never translated to ``\\r\\n`` on Windows.
    JSONL byte offsets must be stable across platforms.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    file_existed = path.exists()
    with path.open("ab") as handle:
        handle.write(line.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    if not file_existed:
        fsync_directory(path.parent)


def append_durable_jsonl_line(path: Path, line: str) -> None:
    """Repair a torn JSONL tail, then append one durable line."""

    repair_jsonl_tail(path)
    append_text_line(path, line)
