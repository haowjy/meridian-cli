"""Garbage collection for orphaned per-spawn coordination locks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from meridian.lib.platform.locking import (
    acquire_file_lock,
    release_file_lock,
    try_lock_file,
    unlink_validated_lock,
)
from meridian.lib.state.spawn.repository import is_safe_spawn_dir_name

_LOCK_CLASSES = ("spawns", "process-scopes", "reaper-cleanup", "launch-boundary")


@dataclass(frozen=True)
class LockGcStats:
    """Counts from one lock-GC pass."""

    files_seen: int = 0
    files_removed: int = 0
    files_contended: int = 0
    pass_skipped: bool = False


def gc_orphaned_locks(runtime_root: Path) -> LockGcStats:
    """Remove orphaned per-spawn locks under validated exclusive acquisitions."""

    gc_lock = runtime_root / "locks" / "gc.lock"
    with try_lock_file(gc_lock, reentrant=False) as gc_handle:
        if gc_handle is None:
            return LockGcStats(pass_skipped=True)

        seen = 0
        removed = 0
        contended = 0
        for lock_class in _LOCK_CLASSES:
            lock_dir = runtime_root / "locks" / lock_class
            try:
                entries = tuple(lock_dir.iterdir())
            except OSError:
                continue
            for lock_path in entries:
                if lock_path.suffix != ".lock":
                    continue
                spawn_id = lock_path.stem
                if not is_safe_spawn_dir_name(spawn_id):
                    continue
                seen += 1
                try:
                    handle = acquire_file_lock(lock_path, timeout=0)
                except (OSError, TimeoutError):
                    contended += 1
                    continue
                try:
                    spawn_path = runtime_root / "spawns" / spawn_id
                    if not spawn_path.exists() and not spawn_path.is_symlink():
                        removed += unlink_validated_lock(lock_path, handle)
                finally:
                    release_file_lock(handle)

        return LockGcStats(
            files_seen=seen,
            files_removed=removed,
            files_contended=contended,
        )


__all__ = ["LockGcStats", "gc_orphaned_locks"]
