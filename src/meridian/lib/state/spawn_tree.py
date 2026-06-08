"""Canonical spawn-tree traversal and descendant cleanup."""

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
    """Yield transitive child rows below ``root_id`` once per spawn id.

    Spawn parent links are persisted data and may be malformed. Treat cycles as
    corrupt rows to walk past, not as a reason for readers to wedge forever.
    The root id starts as visited so a cycle back to the root does not report
    the root as its own descendant.
    """

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
    """Return the root spawn, when present, followed by all descendants."""

    result: list[SpawnRecord] = []
    for row in rows:
        if row.id == root_id:
            result.append(row)
            break
    result.extend(iter_descendants_from_parent_map(root_id, _by_parent(rows)))
    return result


def descendant_id_set(root_id: str, rows: Sequence[SpawnRecord]) -> set[str]:
    """Return transitive descendant ids below ``root_id``, excluding the root."""

    return {row.id for row in iter_descendants_from_parent_map(root_id, _by_parent(rows))}


def has_outstanding_descendant_work(root_id: str, rows: Sequence[SpawnRecord]) -> bool:
    """Return whether any transitive child spawn is still non-terminal.

    This is intentionally a pure spawn-row check. It does not reconcile,
    terminate, or mutate child rows; resident harness drains use it as their
    zero-cooperation tracked-work signal.
    """

    return any(
        is_active_spawn_status(child.status)
        for child in iter_descendants_from_parent_map(root_id, _by_parent(rows))
    )


def active_descendants(
    runtime_root: Path,
    root_id: str | SpawnId,
) -> list[SpawnRecord]:
    """Active (non-terminal) transitive descendants of one spawn."""

    rows = spawn_store.list_spawns(runtime_root)
    return [
        record
        for record in iter_descendants_from_parent_map(str(root_id), _by_parent(rows))
        if is_active_spawn_status(record.status)
    ]


def terminate_recorded_spawn_scope(
    runtime_root: Path,
    record: SpawnRecord,
    *,
    reason: str,
    grace_seconds: float = 5.0,
) -> list[CleanupResult]:
    """Terminate one already-selected spawn record's process scopes."""

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
    "has_outstanding_descendant_work",
    "iter_descendants_from_parent_map",
    "terminate_recorded_spawn_scope",
]
