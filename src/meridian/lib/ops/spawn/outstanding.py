"""Read-only outstanding descendant spawn checks."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.core.types import SpawnId
from meridian.lib.state import spawn_store


def has_outstanding_descendant_work(runtime_root: Path, spawn_id: SpawnId | str) -> bool:
    """Return whether any transitive child spawn is still non-terminal.

    This is intentionally a pure spawn-store read. It does not reconcile,
    terminate, or mutate child rows; resident harness drains use it as their
    zero-cooperation tracked-work signal.
    """

    children = spawn_store.list_spawns(runtime_root, filters={"parent_id": str(spawn_id)})
    queue = [child.id for child in children]
    for child in children:
        if is_active_spawn_status(child.status):
            return True

    while queue:
        parent_id = queue.pop(0)
        descendants = spawn_store.list_spawns(runtime_root, filters={"parent_id": parent_id})
        for descendant in descendants:
            if is_active_spawn_status(descendant.status):
                return True
            queue.append(descendant.id)
    return False


__all__ = ["has_outstanding_descendant_work"]
