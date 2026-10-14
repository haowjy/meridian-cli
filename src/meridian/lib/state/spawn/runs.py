"""Run discovery and sibling grouping, continuation, and retention rules."""

from collections.abc import Iterable, Sequence
from pathlib import Path

from meridian.lib.core.refs import parse_ref, reference_sort_key, spawn_of
from meridian.lib.core.types import RunId, SpawnId
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import scan_spawn_ids


def group_runs(rows: Iterable[SpawnRecord]) -> dict[SpawnId, tuple[SpawnRecord, ...]]:
    groups: dict[SpawnId, list[SpawnRecord]] = {}
    for row in rows:
        groups.setdefault(spawn_of(row.id), []).append(row)
    return {
        key: tuple(sorted(group, key=lambda row: reference_sort_key(row.id)))
        for key, group in groups.items()
    }


def continue_target_run(rows: Sequence[SpawnRecord]) -> SpawnRecord | None:
    """Newest bound run or reopen reservation, never a legacy caller-chat row."""
    return max(
        (row for row in rows if row.session_instance_id is not None
         or (row.owner_chat_id is not None and row.owner_chat_id != row.chat_id)),
        key=lambda row: reference_sort_key(row.id), default=None,
    )


def retention_run_ids(rows: Sequence[SpawnRecord]) -> set[RunId]:
    """Keep latest, latest bound, and the selected continuation source until last."""
    if not rows:
        return set()
    latest = max(rows, key=lambda row: reference_sort_key(row.id))
    bound = max(
        (row for row in rows if row.session_instance_id is not None),
        key=lambda row: reference_sort_key(row.id), default=None,
    )
    target = continue_target_run(rows)
    return {row.id for row in (latest, bound, target) if row is not None}


def retention_dependencies(rows: Sequence[SpawnRecord]) -> dict[RunId, set[RunId]]:
    """Special runs retire only after ordinary runs and earlier special runs."""
    special = retention_run_ids(rows)
    preceding = {row.id for row in rows} - special
    dependencies: dict[RunId, set[RunId]] = {}
    for key in sorted(special, key=reference_sort_key):
        dependencies[key] = preceding.copy()
        preceding.add(key)
    return dependencies


def runs_of(runtime_root: Path, spawn_id: SpawnId) -> tuple[RunId, ...]:
    """Return this spawn's loose run rows in numeric run order.

    Junk and legacy ``rN`` rows remain visible to raw scans but never become
    members of a stable spawn. Holes are valid: the maximum surviving suffix,
    not the row count, identifies the latest run.
    """

    paths = RuntimePaths.from_root_dir(runtime_root)
    matches: list[tuple[int, str, RunId]] = []
    for row_key in scan_spawn_ids(paths.spawns_dir):
        parsed = parse_ref(str(row_key))
        if parsed.kind not in {"spawn", "run"} or parsed.spawn_id != spawn_id:
            continue
        assert parsed.run is not None
        matches.append((parsed.run, str(row_key), row_key))
    return tuple(row_key for _, _, row_key in sorted(matches))


def latest_run(runtime_root: Path, spawn_id: SpawnId) -> RunId | None:
    """Return the highest-numbered loose run for a stable spawn."""

    runs = runs_of(runtime_root, spawn_id)
    return runs[-1] if runs else None
