"""Cross-platform file locking primitives for Meridian state stores."""

from __future__ import annotations

import errno
import os
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, cast

from meridian.lib.platform import IS_WINDOWS, fcntl

_THREAD_LOCAL = threading.local()


def _held_locks() -> dict[Path, tuple[IO[bytes], int]]:
    """Return thread-local map of lock path -> (handle, reentrant depth)."""
    held = cast("dict[Path, tuple[IO[bytes], int]] | None", getattr(_THREAD_LOCAL, "held", None))
    if held is None:
        held = {}
        _THREAD_LOCAL.held = held
    return held


@contextmanager
def lock_file(lock_path: Path) -> Generator[IO[bytes], None, None]:
    """Acquire an exclusive file lock with thread-local reentrancy support."""
    key = lock_path.resolve()
    held = _held_locks()
    existing = held.get(key)
    if existing is not None:
        handle, depth = existing
        held[key] = (handle, depth + 1)
        try:
            yield handle
        finally:
            current_handle, current_depth = held[key]
            if current_depth <= 1:
                held.pop(key, None)
            else:
                held[key] = (current_handle, current_depth - 1)
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if IS_WINDOWS:
            _acquire_windows_lock(handle)
        else:
            _acquire_posix_lock(handle)
        held[key] = (handle, 1)
        try:
            yield handle
        finally:
            held.pop(key, None)
            if IS_WINDOWS:
                _release_windows_lock(handle)
            else:
                _release_posix_lock(handle)


@contextmanager
def try_lock_file(lock_path: Path) -> Generator[IO[bytes] | None, None, None]:
    """Attempt exclusive file lock, non-blocking. Yields None if already held."""
    key = lock_path.resolve()
    held = _held_locks()
    existing = held.get(key)
    if existing is not None:
        handle, depth = existing
        held[key] = (handle, depth + 1)
        try:
            yield handle
        finally:
            current_handle, current_depth = held[key]
            if current_depth <= 1:
                held.pop(key, None)
            else:
                held[key] = (current_handle, current_depth - 1)
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = lock_path.open("a+b")
    except OSError:
        yield None
        return

    try:
        if not _try_acquire_lock(handle):
            yield None
            return
        held[key] = (handle, 1)
        try:
            yield handle
        finally:
            held.pop(key, None)
            if IS_WINDOWS:
                _release_windows_lock(handle)
            else:
                _release_posix_lock(handle)
    finally:
        handle.close()


def _acquire_posix_lock(handle: IO[bytes]) -> None:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _release_posix_lock(handle: IO[bytes]) -> None:
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _try_acquire_lock(handle: IO[bytes]) -> bool:
    if IS_WINDOWS:
        return _try_acquire_windows_lock(handle)
    return _try_acquire_posix_lock(handle)


def _try_acquire_posix_lock(handle: IO[bytes]) -> bool:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return False
        raise


def _acquire_windows_lock(handle: IO[bytes]) -> None:
    import msvcrt as _msvcrt

    msvcrt = cast("Any", _msvcrt)

    # msvcrt locks byte ranges, so pin a 1-byte region at offset 0.
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
        os.fsync(handle.fileno())
    handle.seek(0)
    while True:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            time.sleep(0.05)


def _try_acquire_windows_lock(handle: IO[bytes]) -> bool:
    import msvcrt as _msvcrt

    msvcrt = cast("Any", _msvcrt)

    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
        os.fsync(handle.fileno())
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _release_windows_lock(handle: IO[bytes]) -> None:
    import msvcrt as _msvcrt

    msvcrt = cast("Any", _msvcrt)

    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


__all__ = ["lock_file", "try_lock_file"]
