"""Canonical spawn-tree traversal and descendant cleanup."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Protocol

from meridian.lib.core.refs import spawn_of
from meridian.lib.core.spawn_lifecycle import SpawnReservation, is_active_spawn_status
from meridian.lib.core.types import SpawnId
from meridian.lib.platform.process_scope.base import CleanupResult
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.spawn.model import SpawnRecord


class DescendantRow(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def parent_id(self) -> str | None: ...

    @property
    def status(self) -> str: ...


def iter_run_parent_descendants[Row: DescendantRow](
    root_id: str, rows: Iterable[Row]
) -> Iterator[Row]:
    """Yield concrete runs whose own parent edge puts them below ``root_id``.

    This answers which run rows belong to the root's operational subtree. A
    reopened run with a parent outside that subtree is excluded even when an
    older generation of the same spawn remains below the root.
    """
    materialized = tuple(rows)
    root_spawn = spawn_of(root_id)
    reachable = {root_spawn}
    changed = True
    while changed:
        changed = False
        for row in materialized:
            if row.parent_id is None:
                continue
            child_spawn = spawn_of(row.id)
            if child_spawn in reachable:
                continue
            if spawn_of(row.parent_id) in reachable:
                reachable.add(child_spawn)
                changed = True

    for row in materialized:
        if (
            spawn_of(row.id) != root_spawn
            and row.parent_id is not None
            and spawn_of(row.parent_id) in reachable
        ):
            yield row


def collect_run_parent_subtree(
    root_id: str, rows: Sequence[SpawnRecord]
) -> list[SpawnRecord]:
    """Return root runs plus rows selected by their own transitive parent edge."""

    root_spawn = spawn_of(root_id)
    result = [row for row in rows if spawn_of(row.id) == root_spawn]
    result.extend(iter_run_parent_descendants(root_id, rows))
    return result


def run_parent_descendant_id_set(root_id: str, rows: Sequence[SpawnRecord]) -> set[str]:
    """Return concrete descendant run ids selected by their own parent edge."""

    return {row.id for row in iter_run_parent_descendants(root_id, rows)}


def has_outstanding_descendant_work(root_id: str, rows: Sequence[SpawnRecord]) -> bool:
    """Return whether any transitive child spawn is still non-terminal.

    This is intentionally a pure spawn-row check. It does not reconcile,
    terminate, or mutate child rows; resident harness drains use it as their
    zero-cooperation tracked-work signal.
    """

    return any(
        is_active_spawn_status(child.status)
        for child in iter_run_parent_descendants(root_id, rows)
    )


def active_descendants(
    runtime_root: Path,
    root_id: str | SpawnId,
) -> list[SpawnRecord]:
    """Active (non-terminal) transitive descendants of one spawn."""

    from meridian.lib.state import spawn_store

    rows = spawn_store.list_spawns(runtime_root).records
    return [
        record
        for record in iter_run_parent_descendants(str(root_id), rows)
        if is_active_spawn_status(record.status)
    ]


def _check_reopen_ancestry(
    paths: RuntimePaths,
    spawn_id: SpawnId,
    reservation: SpawnReservation,
) -> None:
    """Reject a reopen cycle using every surviving run's parent edge.

    Unlike operational subtree selection, this answers whether *any* recorded
    lineage could make the reopened spawn its own ancestor. The conservative
    union prevents a later reopen from hiding a cycle through an older run.
    """
    from meridian.lib.state.spawn.repository import read_state
    from meridian.lib.state.spawn.runs import runs_of

    if reservation.parent_id is None:
        return
    pending = [SpawnId(reservation.parent_id)]
    visited: set[SpawnId] = set()
    while pending:
        parent_id = pending.pop()
        if parent_id == spawn_id:
            raise ValueError(f"Cannot reopen ancestor spawn {spawn_id}")
        if parent_id in visited:
            continue
        visited.add(parent_id)

        for run_id in runs_of(paths.root_dir, parent_id):
            parent = read_state(paths.spawns_dir, run_id, include_prompt=False)
            if parent is not None and parent.parent_id is not None:
                pending.append(parent.parent_id)


check_reopen_ancestry = _check_reopen_ancestry


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
    "check_reopen_ancestry",
    "collect_run_parent_subtree",
    "has_outstanding_descendant_work",
    "iter_run_parent_descendants",
    "run_parent_descendant_id_set",
    "terminate_recorded_spawn_scope",
]
