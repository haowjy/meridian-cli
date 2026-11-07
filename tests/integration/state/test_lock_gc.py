"""Orphaned per-spawn coordination-lock collection."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.platform.locking import acquire_file_lock, release_file_lock
from meridian.lib.state.lock_gc import gc_orphaned_locks
from tests.conftest import posix_only

_LOCK_CLASSES = ("spawns", "process-scopes", "reaper-cleanup")


@posix_only
def test_sweeper_skips_held_lock(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    held_lock = runtime_root / "locks" / "spawns" / "p-held.lock"
    unheld_lock = runtime_root / "locks" / "process-scopes" / "p-orphan.lock"
    held_handle = acquire_file_lock(held_lock)
    unheld_handle = acquire_file_lock(unheld_lock)
    release_file_lock(unheld_handle)
    try:
        stats = gc_orphaned_locks(runtime_root)

        assert held_lock.exists()
        assert not unheld_lock.exists()
        assert stats.files_removed == 1
        assert stats.files_contended == 1
        held_handle.write(b"holder-still-valid")
        held_handle.flush()
    finally:
        release_file_lock(held_handle)
