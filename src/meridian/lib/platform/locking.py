"""Cross-platform file locking primitives for Meridian state stores."""

from __future__ import annotations

import errno
import os
import threading
import time
from collections.abc import Generator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import IO, Any, Literal, cast

from meridian.lib.platform import IS_WINDOWS, fcntl

type LockMode = Literal["exclusive", "shared"]
type _HeldLock = tuple[IO[bytes], int, LockMode]

_THREAD_LOCAL = threading.local()
_LOCK_POLL_INTERVAL_SECONDS = 0.05


def _clear_reentrant_registry_after_fork() -> None:
    """Discard reentrancy state copied from the parent process."""

    _THREAD_LOCAL.held = {}


if not IS_WINDOWS and hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_clear_reentrant_registry_after_fork)


def _held_locks() -> dict[Path, _HeldLock]:
    """Return the thread-local map of reentrant locks."""
    held = cast("dict[Path, _HeldLock] | None", getattr(_THREAD_LOCAL, "held", None))
    if held is None:
        held = {}
        _THREAD_LOCAL.held = held
    return held


def _mode_satisfies(held_mode: LockMode, requested_mode: LockMode) -> bool:
    return held_mode == "exclusive" or requested_mode == "shared"


@contextmanager
def lock_file(
    lock_path: Path,
    *,
    timeout: float | None = None,
    mode: LockMode = "exclusive",
    reentrant: bool = True,
) -> Generator[IO[bytes], None, None]:
    """Acquire a file lock whose path must keep a stable inode.

    ``timeout=None`` waits indefinitely; zero makes one non-blocking attempt.
    Reentrancy is thread-local and may be disabled for callers that need an
    independent acquisition. A held shared lock cannot be upgraded in place.
    """
    key = lock_path.resolve()
    held = _held_locks()
    existing = held.get(key) if reentrant else None
    if existing is not None:
        handle, depth, held_mode = existing
        if not _mode_satisfies(held_mode, mode):
            raise ValueError(f"Cannot upgrade reentrant shared lock to exclusive: {lock_path}")
        held[key] = (handle, depth + 1, held_mode)
        try:
            yield handle
        finally:
            current_handle, current_depth, current_mode = held[key]
            if current_depth <= 1:
                held.pop(key, None)
            else:
                held[key] = (current_handle, current_depth - 1, current_mode)
        return

    handle = acquire_file_lock(lock_path, timeout=timeout, mode=mode)
    if reentrant:
        held[key] = (handle, 1, mode)
    try:
        yield handle
    finally:
        if reentrant:
            held.pop(key, None)
        release_file_lock(handle)


@contextmanager
def try_lock_file(
    lock_path: Path,
    *,
    mode: LockMode = "exclusive",
    reentrant: bool = True,
) -> Generator[IO[bytes] | None, None, None]:
    """Attempt a file lock without blocking; yield ``None`` on contention."""
    stack = ExitStack()
    try:
        handle = stack.enter_context(
            lock_file(lock_path, timeout=0, mode=mode, reentrant=reentrant)
        )
    except (OSError, TimeoutError):
        yield None
        return
    with stack:
        yield handle


def acquire_file_lock(
    lock_path: Path,
    *,
    timeout: float | None = None,
    mode: LockMode = "exclusive",
) -> IO[bytes]:
    """Acquire and return a lock handle, retrying if the path inode changed."""
    if timeout is not None and timeout < 0:
        raise ValueError("timeout must be non-negative or None")

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        handle = lock_path.open("a+b")
        try:
            acquired = _acquire_lock(handle, mode=mode, deadline=deadline)
            if acquired:
                try:
                    path_stat = lock_path.stat()
                except FileNotFoundError:
                    path_stat = None
                handle_stat = os.fstat(handle.fileno())
                if path_stat is not None and (
                    handle_stat.st_ino == path_stat.st_ino
                    and handle_stat.st_dev == path_stat.st_dev
                ):
                    return handle
                _release_lock(handle)
        except BaseException:
            handle.close()
            raise
        handle.close()

        if deadline is not None and time.monotonic() >= deadline:
            raise _timeout_error(lock_path, timeout, mode)


def release_file_lock(handle: IO[bytes]) -> None:
    """Release and close a handle returned by :func:`acquire_file_lock`."""
    try:
        _release_lock(handle)
    finally:
        handle.close()


def _timeout_error(lock_path: Path, timeout: float | None, mode: LockMode) -> TimeoutError:
    return TimeoutError(f"Could not acquire {mode} lock within {timeout}s: {lock_path}")


def _acquire_lock(handle: IO[bytes], *, mode: LockMode, deadline: float | None) -> bool:
    if deadline is None:
        _acquire_blocking(handle, mode)
        return True

    while True:
        if _try_acquire_lock(handle, mode):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(_LOCK_POLL_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))


def _acquire_blocking(handle: IO[bytes], mode: LockMode) -> None:
    if IS_WINDOWS:
        while not _try_acquire_windows_lock(handle, mode):
            time.sleep(_LOCK_POLL_INTERVAL_SECONDS)
        return
    flock_mode = fcntl.LOCK_EX if mode == "exclusive" else fcntl.LOCK_SH
    fcntl.flock(handle.fileno(), flock_mode)


def _try_acquire_lock(handle: IO[bytes], mode: LockMode) -> bool:
    if IS_WINDOWS:
        return _try_acquire_windows_lock(handle, mode)
    flock_mode = fcntl.LOCK_EX if mode == "exclusive" else fcntl.LOCK_SH
    try:
        fcntl.flock(handle.fileno(), flock_mode | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return False
        raise


def _ensure_windows_lock_region(handle: IO[bytes]) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
        os.fsync(handle.fileno())
    handle.seek(0)


def _try_acquire_windows_lock(handle: IO[bytes], mode: LockMode) -> bool:
    import msvcrt as _msvcrt

    msvcrt = cast("Any", _msvcrt)
    _ensure_windows_lock_region(handle)
    flag = msvcrt.LK_NBLCK if mode == "exclusive" else msvcrt.LK_NBRLCK
    try:
        msvcrt.locking(handle.fileno(), flag, 1)
        return True
    except OSError:
        return False


def _release_lock(handle: IO[bytes]) -> None:
    if IS_WINDOWS:
        import msvcrt as _msvcrt

        msvcrt = cast("Any", _msvcrt)
        handle.seek(0)
        with suppress(OSError):
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = [
    "LockMode",
    "acquire_file_lock",
    "lock_file",
    "release_file_lock",
    "try_lock_file",
]
