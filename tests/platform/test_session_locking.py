"""POSIX lock-inode identity revalidation."""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meridian.lib.platform import locking
from meridian.lib.platform.locking import acquire_file_lock, release_file_lock
from tests.conftest import posix_only

if TYPE_CHECKING:
    import pytest


def _fork_while_release_is_paused(
    lock_path: Path, ready_fd: int, stop_fd: int
) -> None:
    handle = acquire_file_lock(lock_path)
    release_started = threading.Event()
    allow_release = threading.Event()
    original_release = locking._release_lock

    def paused_release(releasing_handle: Any) -> None:
        release_started.set()
        assert allow_release.wait(timeout=5)
        original_release(releasing_handle)

    locking._release_lock = paused_release
    threading.Thread(target=release_file_lock, args=(handle,)).start()
    assert release_started.wait(timeout=5)
    threading.Thread(target=lambda: (time.sleep(0.2), allow_release.set())).start()

    child_pid = os.fork()
    if child_pid == 0:
        os.write(ready_fd, b"ready")
        os.read(stop_fd, 1)
        os._exit(0)
    os._exit(0)


@posix_only
def test_acquire_file_lock_retries_when_lock_path_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "sessions" / "c123.lock"
    real_open = Path.open

    def _open_and_replace(self: Path, *args: Any, **kwargs: Any) -> Any:
        handle = real_open(self, *args, **kwargs)
        if self == lock_path and not replaced["done"]:
            replaced["done"] = True
            lock_path.unlink(missing_ok=True)
            lock_path.touch()
        return handle

    replaced = {"done": False}

    monkeypatch.setattr(Path, "open", _open_and_replace)

    handle = acquire_file_lock(lock_path)
    try:
        assert replaced["done"] is True
        handle_stat = os.fstat(handle.fileno())
        path_stat = lock_path.stat()
        assert (handle_stat.st_dev, handle_stat.st_ino) == (path_stat.st_dev, path_stat.st_ino)
    finally:
        release_file_lock(handle)


@posix_only
def test_fork_during_release_does_not_inherit_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fork registry must retain a descriptor until it is closed."""
    lock_path = tmp_path / "release.lock"
    del monkeypatch
    ready_read, ready_write = os.pipe()
    stop_read, stop_write = os.pipe()
    worker = multiprocessing.get_context("fork").Process(
        target=_fork_while_release_is_paused,
        args=(lock_path, ready_write, stop_read),
    )
    worker.start()
    os.close(ready_write)
    os.close(stop_read)
    try:
        assert os.read(ready_read, 5) == b"ready"
        deadline = time.monotonic() + 5
        while worker.exitcode is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert worker.exitcode == 0
        contender = acquire_file_lock(lock_path, timeout=0)
        release_file_lock(contender)
    finally:
        os.close(ready_read)
        os.write(stop_write, b"x")
        os.close(stop_write)
        worker.join(timeout=5)
        if worker.exitcode is None:
            worker.terminate()
            worker.join(timeout=5)
