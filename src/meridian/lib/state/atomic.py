"""Crash-safe file write helpers for authoritative Meridian state."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from meridian.lib.platform import IS_WINDOWS


def _fsync_directory(path: Path) -> None:
    """Fsync a directory entry so a completed replace survives a crash."""
    if IS_WINDOWS:
        # NTFS is journaling; replace durability does not require directory fsync.
        return

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def atomic_publish_dir(stage_dir: Path, dest_dir: Path) -> None:
    """Publish a complete staged directory and sync its parent entry."""

    if os.path.lexists(dest_dir):
        raise FileExistsError(f"Refusing to publish over existing destination: {dest_dir}")
    os.replace(stage_dir, dest_dir)
    _fsync_directory(dest_dir.parent)


def atomic_write_text(path: Path, content: str) -> None:
    """Write text via same-directory temp file + fsync + replace."""

    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes via same-directory temp file + fsync + replace."""

    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)

    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


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
        _fsync_directory(path.parent)
