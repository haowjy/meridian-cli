"""Run discovery and sibling grouping, continuation, and retention rules."""

from collections.abc import Iterable, Sequence
from contextlib import suppress
from pathlib import Path

import structlog

from meridian.lib.core.refs import parse_ref, reference_sort_key, spawn_of
from meridian.lib.core.types import RunId, SpawnId
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.event_store import lock_file
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.spawn.model import SpawnRecord

logger = structlog.get_logger(__name__)


def _run_id(spawn_id: SpawnId, run: int) -> RunId:
    return parse_ref(str(spawn_id) if run == 1 else f"{spawn_id}.{run}", row_key=True)


def _run_exists(paths: RuntimePaths, spawn_id: SpawnId, run: int) -> bool:
    return (paths.spawns_dir / _run_id(spawn_id, run) / "state.json").is_file()


def _head_path(paths: RuntimePaths, spawn_id: SpawnId) -> Path:
    # Heads cannot live under spawns/<pN>: run 1 may retire before later runs.
    return paths.root_dir / "spawn-heads" / str(spawn_id)


def _read_int(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        return None
    return value if value >= 1 else None


def _scan_and_rebuild_head(paths: RuntimePaths, spawn_id: SpawnId) -> int | None:
    """Recover one missing derived head under the allocation gate."""
    from meridian.lib.state.spawn.repository import scan_spawn_ids

    with lock_file(paths.spawns_flock):
        current = _read_int(_head_path(paths, spawn_id))
        if current is not None:
            return current
        highest = max(
            (
                parsed.run or 1
                for run_id in scan_spawn_ids(paths.spawns_dir)
                if (parsed := parse_ref(str(run_id))).spawn_id == spawn_id
            ),
            default=None,
        )
        logger.info(
            "spawn_run_head_rebuilt",
            spawn_id=str(spawn_id),
            run=highest,
        )
        if highest is None:
            with suppress(OSError):
                _head_path(paths, spawn_id).unlink()
            return None
        write_run_head(paths, _run_id(spawn_id, highest))
        return highest


def _read_head(paths: RuntimePaths, spawn_id: SpawnId) -> int | None:
    return _read_int(_head_path(paths, spawn_id)) or _scan_and_rebuild_head(paths, spawn_id)


def write_run_head(paths: RuntimePaths, run_id: RunId) -> None:
    """Atomically advance the highest run ever reserved for one spawn."""

    parsed = parse_ref(str(run_id))
    if parsed.spawn_id is None or parsed.run is None:
        return
    head = _head_path(paths, parsed.spawn_id)
    current = _read_int(head)
    if current is not None and parsed.run <= current:
        return
    head.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(head, f"{parsed.run}\n")


def remove_run_head(paths: RuntimePaths, spawn_id: SpawnId) -> None:
    """Remove the derived head after the spawn's last loose run retires."""
    with suppress(OSError):
        _head_path(paths, spawn_id).unlink()


def remove_run_head_if_empty(paths: RuntimePaths, spawn_id: SpawnId) -> None:
    """Remove the head when none of its bounded run keys remain."""
    highest = _read_int(_head_path(paths, spawn_id))
    if highest is not None and not any(
        _run_exists(paths, spawn_id, run) for run in range(1, highest + 1)
    ):
        remove_run_head(paths, spawn_id)


def group_runs(rows: Iterable[SpawnRecord]) -> dict[SpawnId, tuple[SpawnRecord, ...]]:
    groups: dict[SpawnId, list[SpawnRecord]] = {}
    for row in rows:
        groups.setdefault(spawn_of(row.id), []).append(row)
    return {
        key: tuple(sorted(group, key=lambda row: reference_sort_key(row.id)))
        for key, group in groups.items()
    }


def retention_run_ids(rows: Sequence[SpawnRecord]) -> set[RunId]:
    """Keep the latest and newest generation-bound runs until last."""
    if not rows:
        return set()
    latest = max(rows, key=lambda row: reference_sort_key(row.id))
    bound = max(
        (row for row in rows if row.session_instance_id is not None),
        key=lambda row: reference_sort_key(row.id), default=None,
    )
    return {row.id for row in (latest, bound) if row is not None}


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
    if pointed_run is None:
        return None

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


def latest_bound_run(runtime_root: Path, spawn_id: SpawnId) -> RunId | None:
    """Return the newest generation-bound run below the derived run head."""

    from meridian.lib.state.spawn.repository import read_state

    paths = RuntimePaths.from_root_dir(runtime_root)
    latest = latest_run(runtime_root, spawn_id)
    if latest is None:
        return None
    pointed_run = parse_ref(str(latest)).run or 1
    for candidate in range(pointed_run, 0, -1):
        if not _run_exists(paths, spawn_id, candidate):
            continue
        run_id = _run_id(spawn_id, candidate)
        row = read_state(paths.spawns_dir, run_id, include_prompt=False)
        if row is not None and row.session_instance_id is not None:
            return run_id
    return None


def next_run_id(paths: RuntimePaths, spawn_id: SpawnId) -> RunId:
    """Return the next never-reserved key while ``spawns_flock`` is held."""
    highest = _read_head(paths, spawn_id)
    if highest is None:
        raise ValueError(f"Spawn {spawn_id} has no live runs")
    return _run_id(spawn_id, highest + 1)


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
