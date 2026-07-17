"""Dependency-neutral atomic file replacement."""

from __future__ import annotations

import os
import stat
import uuid
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager, suppress
from pathlib import Path
from typing import IO, Literal, overload

from meridian.lib.platform import IS_WINDOWS


class AtomicReplaceDurabilityError(OSError):
    """The replacement committed, but syncing its directory entry failed."""

    committed = True

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"Atomic replacement committed but directory sync failed: {path}")


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
    path: Path,
    *,
    mode: Literal["w"] = "w",
    encoding: str = "utf-8",
    durable: bool = True,
    permissions: int | Literal["preserve"] = "preserve",
) -> AbstractContextManager[IO[str]]: ...


@overload
def atomic_replace(
    path: Path,
    *,
    mode: Literal["wb"],
    encoding: None = None,
    durable: bool = True,
    permissions: int | Literal["preserve"] = "preserve",
) -> AbstractContextManager[IO[bytes]]: ...


def _open_unique_temp(path: Path, creation_mode: int) -> tuple[int, Path]:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    for _attempt in range(100):
        tmp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            return os.open(tmp_path, flags, creation_mode), tmp_path
        except FileExistsError:
            continue
    raise FileExistsError(f"Could not create unique temporary file for {path}")


@contextmanager
def atomic_replace(
    path: Path,
    *,
    mode: Literal["w", "wb"] = "w",
    encoding: str | None = "utf-8",
    durable: bool = True,
    permissions: int | Literal["preserve"] = "preserve",
) -> Generator[IO[str] | IO[bytes], None, None]:
    """Yield a unique same-directory temp file and replace ``path`` on clean exit.

    Exceptions before replacement leave the previous destination intact and remove the
    temporary file. By default an existing destination's permission bits are preserved;
    a new file receives mode ``0o666`` filtered through the process umask. Pass an
    integer permission mode when the caller owns a stricter file policy.
    Set ``durable=False`` for ephemeral cross-process publication that needs atomicity
    but not crash durability.
    """

    if permissions != "preserve" and (
        isinstance(permissions, bool) or permissions < 0 or permissions > 0o7777
    ):
        raise ValueError("permissions must be 'preserve' or a mode from 0o0000 to 0o7777")

    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode: int | None = None
    if permissions == "preserve":
        with suppress(FileNotFoundError):
            existing_mode = stat.S_IMODE(path.stat().st_mode)
    creation_mode = 0o666 if permissions == "preserve" and existing_mode is None else 0o600
    fd, tmp_path = _open_unique_temp(path, creation_mode)
    if isinstance(permissions, int):
        os.fchmod(fd, permissions)
    elif existing_mode is not None:
        os.fchmod(fd, existing_mode)
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
            try:
                fsync_directory(path.parent)
            except OSError as error:
                raise AtomicReplaceDurabilityError(path) from error
    finally:
        tmp_path.unlink(missing_ok=True)


__all__ = ["AtomicReplaceDurabilityError", "atomic_replace", "fsync_directory"]
