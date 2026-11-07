from __future__ import annotations

import os
from pathlib import Path

from meridian.lib.platform.atomic import atomic_replace, fsync_directory


def atomic_publish_dir(stage_dir: Path, dest_dir: Path) -> None:

    if os.path.lexists(dest_dir):
        raise FileExistsError(f"Refusing to publish over existing destination: {dest_dir}")
    os.replace(stage_dir, dest_dir)
    fsync_directory(dest_dir.parent)


def atomic_write_text(path: Path, content: str) -> None:

    with atomic_replace(path, permissions=0o600) as handle:
        handle.write(content)


def append_text_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_existed = path.exists()
    # Binary mode keeps JSONL byte offsets stable across platforms.
    with path.open("ab") as handle:
        handle.write(line.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    if not file_existed:
        fsync_directory(path.parent)
