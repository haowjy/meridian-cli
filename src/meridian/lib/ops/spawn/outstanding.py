"""Read-only outstanding descendant spawn checks."""

from __future__ import annotations

from collections import deque
from pathlib import Path

from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.core.types import SpawnId
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

    queue: deque[str] = deque([str(spawn_id)])
    while queue:
        parent_id = queue.popleft()
        for child in by_parent.get(parent_id, []):
            if is_active_spawn_status(child.status):
                return True
            queue.append(child.id)
    return False


__all__ = ["has_outstanding_descendant_work"]
