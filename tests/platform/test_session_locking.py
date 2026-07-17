"""POSIX lock-inode identity revalidation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meridian.lib.platform.locking import acquire_file_lock, release_file_lock
from tests.conftest import posix_only

if TYPE_CHECKING:
    import pytest


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
