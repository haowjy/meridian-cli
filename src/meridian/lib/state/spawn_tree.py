"""Canonical spawn-tree traversal and descendant cleanup."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Protocol

from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.core.types import SpawnId
from meridian.lib.platform.process_scope.base import CleanupResult
from meridian.lib.state import spawn_store
from meridian.lib.state.spawn.model import SpawnRecord


class DescendantRow(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def parent_id(self) -> str | None: ...

    @property
    def status(self) -> str: ...


def _spawn_key(value: str) -> SpawnId:
    """Return a stable run prefix without constructing a ParsedRef per row."""

    normalized = value.strip()
    head, separator, suffix = normalized.partition(".")
    if (
        head.startswith("p")
        and head[1:].isdigit()
        and (not separator or (suffix.isdigit() and int(suffix) >= 1))
    ):
        return SpawnId(head)
    return SpawnId(normalized)


def iter_descendants[Row: DescendantRow](root_id: str, rows: Iterable[Row]) -> Iterator[Row]:
    """Yield all runs of transitive child spawns, tolerating parent cycles.

    Any run's parent edge makes a spawn reachable; keep every run of that
    spawn so an older active row cannot disappear behind a terminal run.
    """
    rows_by_spawn: dict[SpawnId, list[Row]] = {}
    children_by_parent: dict[SpawnId, list[SpawnId]] = {}
    for row in rows:
        child_spawn = _spawn_key(row.id)
        rows_by_spawn.setdefault(child_spawn, []).append(row)
        if row.parent_id is not None:
            children_by_parent.setdefault(_spawn_key(row.parent_id), []).append(child_spawn)
    root_spawn = _spawn_key(root_id)
    visited = {root_spawn}
    stack = [root_spawn]
    while stack:
        for child_spawn in children_by_parent.get(stack.pop(), ()):
            if child_spawn in visited:
                continue
            visited.add(child_spawn)
            yield from rows_by_spawn[child_spawn]
            stack.append(child_spawn)


def collect_descendants(root_id: str, rows: Sequence[SpawnRecord]) -> list[SpawnRecord]:
    """Return all root run rows, when present, followed by descendant run rows."""

    root_spawn = _spawn_key(root_id)
    result = [row for row in rows if _spawn_key(row.id) == root_spawn]
    result.extend(iter_descendants(root_id, rows))
    return result


def descendant_id_set(root_id: str, rows: Sequence[SpawnRecord]) -> set[str]:
    """Return transitive descendant ids below ``root_id``, excluding the root."""

    return {row.id for row in iter_descendants(root_id, rows)}


def has_outstanding_descendant_work(root_id: str, rows: Sequence[SpawnRecord]) -> bool:
    """Return whether any transitive child spawn is still non-terminal.

    This is intentionally a pure spawn-row check. It does not reconcile,
    terminate, or mutate child rows; resident harness drains use it as their
    zero-cooperation tracked-work signal.
    """

    return any(
        is_active_spawn_status(child.status)
        for child in iter_descendants(root_id, rows)
    )


def active_descendants(
    runtime_root: Path,
    root_id: str | SpawnId,
) -> list[SpawnRecord]:
    """Active (non-terminal) transitive descendants of one spawn."""

    rows = spawn_store.list_spawns(runtime_root).records
    return [
        record
        for record in iter_descendants(str(root_id), rows)
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
    "iter_descendants",
    "terminate_recorded_spawn_scope",
]
