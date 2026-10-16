"""Canonical spawn-tree traversal and descendant cleanup."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Protocol
from uuid import UUID

from meridian.lib.core.process_cleanup import terminate_spawn_scopes
from meridian.lib.core.refs import spawn_of
from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.core.types import SpawnId
from meridian.lib.platform.process_scope.base import CleanupResult
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import read_state
from meridian.lib.state.spawn.runs import runs_of


class DescendantRow(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def parent_id(self) -> str | None: ...

    @property
    def history_id(self) -> UUID | None: ...

    @property
    def parent_history_id(self) -> UUID | None: ...

    @property
    def status(self) -> str: ...


def iter_run_parent_descendants[Row: DescendantRow](
    root_id: str, rows: Iterable[Row]
) -> Iterator[Row]:
    """Yield concrete runs whose exact parent run puts them below ``root_id``.

    Rows predating exact parent history fall back to their stable spawn edge.
    A reopened run with a parent outside the subtree, and children launched by
    that run, stay excluded even when an older generation remains below the root.
    """
    materialized = tuple(rows)
    root_spawn = spawn_of(root_id)
    selected_runs = {row.id for row in materialized if spawn_of(row.id) == root_spawn}
    selected_histories = {
        row.history_id
        for row in materialized
        if row.id in selected_runs and row.history_id is not None
    }
    selected_spawns = {root_spawn}
    changed = True
    while changed:
        changed = False
        for row in materialized:
            if row.id in selected_runs:
                continue
            if row.parent_history_id is not None:
                selected = row.parent_history_id in selected_histories
            else:
                selected = (
                    row.parent_id is not None
                    and spawn_of(row.parent_id) in selected_spawns
                )
            if not selected:
                continue
            selected_runs.add(row.id)
            if row.history_id is not None:
                selected_histories.add(row.history_id)
            selected_spawns.add(spawn_of(row.id))
            changed = True

    for row in materialized:
        if row.id in selected_runs and spawn_of(row.id) != root_spawn:
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


def check_reopen_ancestry(
    paths: RuntimePaths,
    spawn_id: SpawnId,
    parent_id: str | None,
) -> None:
    """Reject a reopen cycle using every surviving run's parent edge.

    Unlike operational subtree selection, this answers whether *any* recorded
    lineage could make the reopened spawn its own ancestor. The conservative
    union prevents a later reopen from hiding a cycle through an older run.
    """
    if parent_id is None:
        return
    pending = [SpawnId(parent_id)]
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

def terminate_recorded_spawn_scope(
    runtime_root: Path,
    record: SpawnRecord,
    *,
    reason: str,
    grace_seconds: float = 5.0,
) -> list[CleanupResult]:
    """Terminate one already-selected spawn record's process scopes."""

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
