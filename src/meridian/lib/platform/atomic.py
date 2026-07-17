"""Dependency-neutral atomic file replacement."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import IO, Literal, overload

from meridian.lib.platform import IS_WINDOWS


def fsync_directory(path: Path) -> None:
    """Sync a directory entry so a completed replace survives a crash."""

    if IS_WINDOWS:
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@overload
def atomic_replace(
    path: Path, *, mode: Literal["w"] = "w", encoding: str = "utf-8", durable: bool = True
) -> AbstractContextManager[IO[str]]: ...


@overload
def atomic_replace(
    path: Path, *, mode: Literal["wb"], encoding: None = None, durable: bool = True
) -> AbstractContextManager[IO[bytes]]: ...


@contextmanager
def atomic_replace(
    path: Path,
    *,
    mode: Literal["w", "wb"] = "w",
    encoding: str | None = "utf-8",
    durable: bool = True,
) -> Generator[IO[str] | IO[bytes], None, None]:
    """Yield a unique same-directory temp file and replace ``path`` on clean exit.

    Exceptions leave the previous destination intact and remove the temporary file.
    Set ``durable=False`` for ephemeral cross-process publication that needs atomicity
    but not crash durability.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        if mode == "wb":
            handle: IO[str] | IO[bytes] = os.fdopen(fd, mode)
        else:
            handle = os.fdopen(fd, mode, encoding=encoding)
        with handle:
            yield handle
            handle.flush()
            if durable:
                os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        if durable:
            fsync_directory(path.parent)
    finally:
        tmp_path.unlink(missing_ok=True)


__all__ = ["atomic_replace", "fsync_directory"]
