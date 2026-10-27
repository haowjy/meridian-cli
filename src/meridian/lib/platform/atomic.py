"""Dependency-neutral atomic file replacement."""

from __future__ import annotations

import os
import stat
import time
import uuid
from collections.abc import Generator, Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from pathlib import Path
from typing import IO, Literal, overload

from meridian.lib.platform import IS_WINDOWS

_ATOMIC_TEMP_SUFFIX = ".tmp"
# A publisher that crashed left its staged link behind; younger ones may belong
# to a live publisher, so leave those alone.
_STAGED_LINK_MAX_AGE_SECONDS = 3600.0


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


def is_atomic_temp_name(name: str, filename: str) -> bool:
    """Whether ``name`` is a same-directory staging temp staged for ``filename``."""
    return name.startswith(f".{filename}.") and name.endswith(_ATOMIC_TEMP_SUFFIX)


def iter_atomic_temp_paths(directory: Path, filename: str) -> Iterator[Path]:
    """Yield stale staging temps for ``filename``; the caller holds the source guard."""
    return directory.glob(f".{filename}.*{_ATOMIC_TEMP_SUFFIX}")


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


def atomic_write_text(path: Path, content: str, *, durable: bool = True) -> None:
    """Atomically replace user-owned text while preserving an existing mode."""

    with atomic_replace(path, durable=durable) as handle:
        handle.write(content)


def atomic_symlink(link: Path, target: Path) -> Path | None:
    """Point ``link`` at ``target``, replacing it atomically; return what was displaced.

    Build the new link under a temporary name in the same directory, then rename it
    over the old one, so a crash leaves either the old link or the new one. A link
    that already names ``target`` is left alone. A real file or directory in the
    slot — a writer that wrote the path instead of resolving it — is moved aside and
    returned, because only a symlink can be swapped over. Staged links a crashed
    publisher left behind are swept.
    """

    link.parent.mkdir(parents=True, exist_ok=True)
    wanted = target.as_posix()
    if link.is_symlink() and os.readlink(link) == wanted:
        return None
    name = link.name
    cutoff = time.time() - _STAGED_LINK_MAX_AGE_SECONDS
    for staged_link in link.parent.glob(f"{name}.*{_ATOMIC_TEMP_SUFFIX}"):
        with suppress(OSError):
            if staged_link.lstat().st_mtime < cutoff:
                staged_link.unlink()
    staged = link.parent / f"{name}.{os.getpid()}.{uuid.uuid4().hex}{_ATOMIC_TEMP_SUFFIX}"
    os.symlink(wanted, staged)
    displaced: Path | None = None
    try:
        if os.path.lexists(link) and not link.is_symlink():
            displaced = link.with_name(f"{name}.displaced.{uuid.uuid4().hex}")
            os.rename(link, displaced)
        os.replace(staged, link)
    except OSError:
        staged.unlink(missing_ok=True)
        raise
    return displaced


__all__ = [
    "AtomicReplaceDurabilityError",
    "atomic_replace",
    "atomic_symlink",
    "atomic_write_text",
    "fsync_directory",
    "is_atomic_temp_name",
    "iter_atomic_temp_paths",
]
