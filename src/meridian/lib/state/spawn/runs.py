"""Run discovery and sibling grouping, continuation, and retention rules."""

from collections.abc import Iterable, Sequence
from pathlib import Path

from meridian.lib.core.refs import parse_ref, reference_sort_key, spawn_of
from meridian.lib.core.types import RunId, SpawnId
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.spawn.model import SpawnRecord


def _run_id(spawn_id: SpawnId, run: int) -> RunId:
    return parse_ref(str(spawn_id) if run == 1 else f"{spawn_id}.{run}", row_key=True)


def _run_exists(paths: RuntimePaths, spawn_id: SpawnId, run: int) -> bool:
    return (paths.spawns_dir / _run_id(spawn_id, run) / "state.json").is_file()


def _head_path(paths: RuntimePaths, spawn_id: SpawnId) -> Path:
    # Heads cannot live under spawns/<pN>: run 1 may retire before later runs.
    return paths.root_dir / "spawn-heads" / str(spawn_id)


def _read_head(paths: RuntimePaths, spawn_id: SpawnId) -> int:
    try:
        run = int(_head_path(paths, spawn_id).read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        return 1
    return max(run, 1)


def write_run_head(paths: RuntimePaths, run_id: RunId) -> None:
    """Atomically record a derived upper bound before publishing a later run."""

    parsed = parse_ref(str(run_id))
    assert parsed.spawn_id is not None and parsed.run is not None
    if parsed.run == 1:
        return
    head = _head_path(paths, parsed.spawn_id)
    head.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(head, f"{parsed.run}\n")


def group_runs(rows: Iterable[SpawnRecord]) -> dict[SpawnId, tuple[SpawnRecord, ...]]:
    groups: dict[SpawnId, list[SpawnRecord]] = {}
    for row in rows:
        groups.setdefault(spawn_of(row.id), []).append(row)
    return {
        key: tuple(sorted(group, key=lambda row: reference_sort_key(row.id)))
        for key, group in groups.items()
    }


def continue_target_run(rows: Sequence[SpawnRecord]) -> SpawnRecord | None:
    """Newest run that actually acquired a session generation."""
    return max(
        (row for row in rows if row.session_instance_id is not None),
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
    latest = latest_run(runtime_root, spawn_id)
    if latest is None:
        return ()
    last_run = parse_ref(str(latest)).run
    assert last_run is not None
    return tuple(
        _run_id(spawn_id, run)
        for run in range(1, last_run + 1)
        if _run_exists(paths, spawn_id, run)
    )


def latest_run(runtime_root: Path, spawn_id: SpawnId) -> RunId | None:
    """Return the newest loose run without scanning unrelated spawn directories."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    pointed_run = _read_head(paths, spawn_id)

    # A stale pointer can be left by an older/pointerless writer. Probe upward
    # first so the derived cache is never required for correctness.
    run = pointed_run
    while _run_exists(paths, spawn_id, run + 1):
        run += 1
    if _run_exists(paths, spawn_id, run):
        return _run_id(spawn_id, run)

    # reserve_run publishes the pointer before the row. A crash in between is
    # recovered by finding the highest surviving predecessor.
    for candidate in range(run - 1, 0, -1):
        if _run_exists(paths, spawn_id, candidate):
            return _run_id(spawn_id, candidate)
    return None


class SpawnBusy(ValueError):
    """A reopen would overlap an active run of the same spawn."""

    def __init__(self, run_id: RunId):
        self.run_id = run_id
        spawn = spawn_of(run_id)
        super().__init__(
            f"Spawn {spawn} is busy (run {run_id}); use `spawn inject {spawn}` "
            f"or `--fork {spawn}`."
        )


def check_spawn_available(
    runtime_root: Path, spawn_id: SpawnId, chat_id: str | None = None,
) -> RunId:
    """Read-only preflight, repeated under the allocation gate before publication."""
    from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
    from meridian.lib.state.reaper import peek_reconciled_active_spawn
    from meridian.lib.state.session_store import ChatBusy, is_session_lease_owner_alive
    from meridian.lib.state.spawn.repository import read_state

    latest = latest_run(runtime_root, spawn_id)
    if latest is None:
        raise ValueError(
            f"Spawn {spawn_id} has no live runs (not found, or archived: "
            f"restore it or `--fork {spawn_id}`)"
        )
    row = read_state(
        RuntimePaths.from_root_dir(runtime_root).spawns_dir, latest, include_prompt=False,
    )
    if row is None:
        raise ValueError(f"Latest run unavailable: {latest}; run `meridian doctor`")
    if is_active_spawn_status(peek_reconciled_active_spawn(runtime_root, row).status):
        raise SpawnBusy(latest)
    if chat_id is not None and is_session_lease_owner_alive(runtime_root, chat_id):
        raise ChatBusy(chat_id)
    return latest
