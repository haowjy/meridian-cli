from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.core.types import SpawnId
from meridian.lib.platform.process_scope.base import CleanupResult
from meridian.lib.state import spawn_store
from meridian.lib.state.spawn.model import SpawnRecord


def iter_descendants_from_parent_map(
    root_id: str,
    by_parent: Mapping[str | None, Sequence[SpawnRecord]],
) -> Iterator[SpawnRecord]:
    # Persisted parent links may contain cycles, including one back to the root.
    visited = {root_id}
    stack = [root_id]
    while stack:
        parent_id = stack.pop()
        for child in by_parent.get(parent_id, ()):
            child_id = child.id
            if child_id in visited:
                continue
            visited.add(child_id)
            yield child
            stack.append(child_id)


def _by_parent(rows: Sequence[SpawnRecord]) -> dict[str | None, list[SpawnRecord]]:
    by_parent: dict[str | None, list[SpawnRecord]] = {}
    for row in rows:
        by_parent.setdefault(row.parent_id, []).append(row)
    return by_parent


def collect_descendants(root_id: str, rows: Sequence[SpawnRecord]) -> list[SpawnRecord]:

    result: list[SpawnRecord] = []
    for row in rows:
        if row.id == root_id:
            result.append(row)
            break
    result.extend(iter_descendants_from_parent_map(root_id, _by_parent(rows)))
    return result


def descendant_id_set(root_id: str, rows: Sequence[SpawnRecord]) -> set[str]:

    return {row.id for row in iter_descendants_from_parent_map(root_id, _by_parent(rows))}


def active_descendants(
    runtime_root: Path,
    root_id: str | SpawnId,
) -> list[SpawnRecord]:
    """Selection for convergence uses stored status; liveness questions use read_derived."""

    rows = spawn_store.list_spawns(runtime_root).records
    return [
        child
        for child in iter_descendants_from_parent_map(str(root_id), _by_parent(rows))
        if is_active_spawn_status(child.status)
    ]


def terminate_recorded_spawn_scope(
    runtime_root: Path,
    record: SpawnRecord,
    *,
    reason: str,
    grace_seconds: float = 5.0,
) -> list[CleanupResult]:

    from meridian.lib.core.process_cleanup import terminate_spawn_scopes

    return terminate_spawn_scopes(
        runtime_root,
        record,
        reason=reason,
        grace_seconds=grace_seconds,
    )


__all__ = [
    "active_descendants",
    "collect_descendants",
    "descendant_id_set",
    "iter_descendants_from_parent_map",
    "terminate_recorded_spawn_scope",
]
