"""Orphaned per-spawn coordination-lock collection."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.platform.locking import acquire_file_lock, release_file_lock
from meridian.lib.state import spawn_store
from meridian.lib.state.lock_gc import gc_orphaned_locks
from tests.conftest import posix_only

_LOCK_CLASSES = ("spawns", "process-scopes", "reaper-cleanup", "launch-boundary")


def _start_spawn(runtime_root: Path, spawn_id: str) -> None:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c1",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="hello",
        status="succeeded",
    )


@posix_only
def test_sweeper_removes_orphaned_spawn_lock_classes(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = "p1"
    _start_spawn(runtime_root, spawn_id)
    lock_paths = tuple(
        runtime_root / "locks" / lock_class / f"{spawn_id}.lock"
        for lock_class in _LOCK_CLASSES
    )
    for lock_path in lock_paths:
        handle = acquire_file_lock(lock_path)
        release_file_lock(handle)

    assert spawn_store.delete_published_spawn(
        runtime_root,
        spawn_id,
        can_delete=lambda _record: True,
    )

    stats = gc_orphaned_locks(runtime_root)

    assert all(not lock_path.exists() for lock_path in lock_paths)
    assert (runtime_root / "locks" / "gc.lock").is_file()
    assert stats.files_removed == 4


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
