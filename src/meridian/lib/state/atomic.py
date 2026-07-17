"""Crash-safe file write helpers for authoritative Meridian state."""

from __future__ import annotations

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

    with atomic_replace(path) as handle:
        handle.write(content)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes via same-directory temp file + fsync + replace."""

    with atomic_replace(path, mode="wb", encoding=None) as handle:
        handle.write(data)


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
