"""Read-only outstanding descendant spawn checks."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.core.types import SpawnId
from meridian.lib.ops.spawn.descendants import iter_descendants_from_parent_map
from meridian.lib.state import spawn_store
from meridian.lib.state.spawn.model import SpawnRecord


def has_outstanding_descendant_work(runtime_root: Path, spawn_id: SpawnId | str) -> bool:
    """Return whether any transitive child spawn is still non-terminal.

    This is intentionally a pure spawn-store read. It does not reconcile,
    terminate, or mutate child rows; resident harness drains use it as their
    zero-cooperation tracked-work signal.
    """

    by_parent: dict[str | None, list[SpawnRecord]] = {}
    for row in spawn_store.list_spawns(runtime_root):
        by_parent.setdefault(row.parent_id, []).append(row)

    for child in iter_descendants_from_parent_map(str(spawn_id), by_parent):
        if is_active_spawn_status(child.status):
            return True
    return False


__all__ = ["has_outstanding_descendant_work"]
