"""Archive-state helpers for spawn visibility."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import atomic_write_text

type ArchiveMutator = Callable[[set[str]], bool]


def archived_spawns_path(runtime_root: Path) -> Path:
    """Path to the archived spawns JSON file."""
    return runtime_root / "app" / "archived_spawns.json"


def archived_spawns_lock_path(runtime_root: Path) -> Path:
    """Lock path for archived spawns file."""
    return runtime_root / "locks" / "archived-spawns.lock"


def _read_archived_spawns(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            raw_items = cast("list[object]", data)
            return {item for item in raw_items if isinstance(item, str)}
        return set()
    except (json.JSONDecodeError, OSError):
        return set()


def read_archived_spawns(runtime_root: Path) -> set[str]:
    """Read the set of archived spawn IDs."""
    path = archived_spawns_path(runtime_root)
    lock_path = archived_spawns_lock_path(runtime_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_file(lock_path, mode="shared"):
        return _read_archived_spawns(path)


def mutate_archived_spawns(
    runtime_root: Path,
    mutator: ArchiveMutator,
) -> bool:
    """Mutate the archive set atomically and return whether it changed.

    Mutation is deliberately non-reentrant: nesting another archive mutation could
    write from an inner snapshot and silently clobber the outer mutation.
    """
    path = archived_spawns_path(runtime_root)
    lock_path = archived_spawns_lock_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    with lock_file(lock_path, reentrant=False):
        archived = _read_archived_spawns(path)
        changed = mutator(archived)
        if changed:
            atomic_write_text(path, json.dumps(sorted(archived), indent=2) + "\n")
        return changed


def archive_spawn(runtime_root: Path, spawn_id: str) -> bool:
    """Add a spawn ID, returning whether it was newly archived."""

    def add_spawn_id(archived: set[str]) -> bool:
        if spawn_id in archived:
            return False
        archived.add(spawn_id)
        return True

    return mutate_archived_spawns(runtime_root, add_spawn_id)


__all__ = [
    "archive_spawn",
    "archived_spawns_lock_path",
    "archived_spawns_path",
    "mutate_archived_spawns",
    "read_archived_spawns",
]
