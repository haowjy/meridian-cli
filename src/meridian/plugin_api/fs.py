"""Filesystem primitives exposed to plugins."""

from __future__ import annotations

import os
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import IO, Any, Literal, cast

from meridian.lib.platform import IS_WINDOWS, fcntl
from meridian.lib.platform.atomic import atomic_replace


def atomic_write_text(path: Path, content: str, *, durable: bool = True) -> None:
    """Atomically replace a text file through the dependency-neutral platform seam."""

    with atomic_replace(path, durable=durable) as handle:
        handle.write(content)


@contextmanager
def file_lock(
    path: Path | str,
    *,
    timeout: float = 60.0,
    mode: Literal["exclusive", "shared"] = "exclusive",
) -> Generator[None, None, None]:
    """Acquire a cross-platform file lock with timeout.

    Raises:
        TimeoutError: If the lock cannot be acquired before timeout expires.
    """

    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    start = time.monotonic()
    handle: IO[bytes] | None = None
    acquired = False

    try:
        handle = lock_path.open("a+b")
        while True:
            try:
                _acquire_lock(handle, mode)
                acquired = True
                break
            except (BlockingIOError, OSError) as exc:
                if time.monotonic() - start > timeout:
                    raise TimeoutError(
                        f"Could not acquire {mode} lock within {timeout}s: {lock_path}"
                    ) from exc
                time.sleep(0.1)

        if mode == "exclusive":
            # PID in lock file is useful for lock contention debugging.
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()).encode("utf-8"))
            handle.flush()

        yield
    finally:
        if handle is not None:
            if acquired:
                _release_lock(handle)
            handle.close()


def _acquire_lock(handle: IO[bytes], mode: Literal["exclusive", "shared"]) -> None:
    """Acquire a non-blocking lock for an opened lock file."""

    if IS_WINDOWS:
        import msvcrt as _msvcrt

        msvcrt = cast("Any", _msvcrt)
        handle.seek(0)
        lock_flag = msvcrt.LK_NBLCK if mode == "exclusive" else msvcrt.LK_NBRLCK
        msvcrt.locking(handle.fileno(), lock_flag, 1)
        return

    flock_mode = fcntl.LOCK_EX if mode == "exclusive" else fcntl.LOCK_SH
    fcntl.flock(handle.fileno(), flock_mode | fcntl.LOCK_NB)


def _release_lock(handle: IO[bytes]) -> None:
    """Release a previously acquired file lock."""

    if IS_WINDOWS:
        import msvcrt as _msvcrt

        msvcrt = cast("Any", _msvcrt)
        handle.seek(0)
        with suppress(OSError):
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
