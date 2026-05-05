"""File-backed spawn event store for a Meridian state root's `spawns.jsonl`.

Also includes file-backed ID generation for spawns and sessions.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from meridian.lib.core.clock import Clock, RealClock
from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.spawn_lifecycle import (
    ACTIVE_SPAWN_STATUSES as _ACTIVE_SPAWN_STATUSES,
)
from meridian.lib.core.spawn_lifecycle import (
    TERMINAL_SPAWN_STATUSES as _TERMINAL_SPAWN_STATUSES,
)
from meridian.lib.core.spawn_lifecycle import (
    is_active_spawn_status as _is_active_spawn_status,
)
from meridian.lib.core.spawn_lifecycle import (
    validate_transition as _validate_transition,
)
from meridian.lib.core.types import SpawnId
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.event_store import lock_file
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.spawn.events import reduce_events

logger = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from meridian.lib.state.spawn.repository import SpawnRepository

# ---------------------------------------------------------------------------
# ID generation (absorbed from state/id_gen.py)
# ---------------------------------------------------------------------------


def _resolve_repository(
    paths: RuntimePaths,
    *,
    repository: SpawnRepository | None = None,
) -> SpawnRepository:
    if repository is not None:
        return repository

    from meridian.lib.state.spawn.repository import FileSpawnRepository

    return FileSpawnRepository(paths)


def _next_spawn_id_from_events(events: list[SpawnEvent]) -> SpawnId:
    starts = sum(1 for event in events if event.event == "start")
    return SpawnId(f"p{starts + 1}")


def _spawn_id_index(spawn_id: SpawnId | str) -> int:
    value = str(spawn_id)
    if len(value) > 1 and value[0] == "p" and value[1:].isdigit():
        return int(value[1:])
    return 0


def _spawn_counter_path(paths: RuntimePaths) -> Path:
    return paths.root_dir / "spawn-id-counter"


def _read_spawn_counter(paths: RuntimePaths) -> int:
    counter_path = _spawn_counter_path(paths)
    if not counter_path.is_file():
        return 0
    try:
        return int(counter_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _seed_spawn_counter_from_events(events: list[SpawnEvent]) -> int:
    return max((_spawn_id_index(getattr(event, "id", "")) for event in events), default=0)


def reserve_spawn_id(
    runtime_root: Path,
    *,
    repository: SpawnRepository | None = None,
) -> SpawnId:
    """Reserve and return the next spawn ID (`p1`, `p2`, ...).

    Reservation is persisted separately from spawn rows so callers can compose
    launch context with the final ID before writing lifecycle-visible events.
    """

    paths = RuntimePaths.from_root_dir(runtime_root)
    resolved_repository = _resolve_repository(paths, repository=repository)
    with lock_file(paths.spawns_flock):
        current = _read_spawn_counter(paths)
        if current == 0 and not _spawn_counter_path(paths).is_file():
            current = _seed_spawn_counter_from_events(resolved_repository.read_events())
        next_value = current + 1
        atomic_write_text(_spawn_counter_path(paths), f"{next_value}\n")
        return SpawnId(f"p{next_value}")


def next_spawn_id(
    runtime_root: Path,
    *,
    repository: SpawnRepository | None = None,
) -> SpawnId:
    """Return the next spawn ID (`p1`, `p2`, ...) for a state root."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    resolved_repository = _resolve_repository(paths, repository=repository)
    with lock_file(paths.spawns_flock):
        next_from_events = _next_spawn_id_from_events(resolved_repository.read_events())
        next_from_counter = SpawnId(f"p{_read_spawn_counter(paths) + 1}")
        return max(
            (next_from_events, next_from_counter),
            key=_spawn_id_index,
        )


LaunchMode = Literal["background", "foreground", "app"]
BACKGROUND_LAUNCH_MODE: LaunchMode = "background"
FOREGROUND_LAUNCH_MODE: LaunchMode = "foreground"
APP_LAUNCH_MODE: LaunchMode = "app"
SpawnOrigin = Literal["runner", "launcher", "launch_failure", "cancel", "reconciler"]
_LAUNCH_MODE_VALUES: frozenset[LaunchMode] = frozenset(
    (BACKGROUND_LAUNCH_MODE, FOREGROUND_LAUNCH_MODE, APP_LAUNCH_MODE)
)

_AUTHORITATIVE_ORIGIN_VALUES: tuple[SpawnOrigin, ...] = (
    "runner",
    "launcher",
    "launch_failure",
    "cancel",
)
AUTHORITATIVE_ORIGINS: frozenset[SpawnOrigin] = frozenset(_AUTHORITATIVE_ORIGIN_VALUES)

ACTIVE_SPAWN_STATUSES = _ACTIVE_SPAWN_STATUSES
is_active_spawn_status = _is_active_spawn_status


# ---------------------------------------------------------------------------
# Spawn event store
# ---------------------------------------------------------------------------


class SpawnRecord(BaseModel):
    """Derived spawn state assembled from spawn JSONL events."""

    model_config = ConfigDict(frozen=True)

    id: str
    chat_id: str | None
    parent_id: str | None
    model: str | None
    agent: str | None
    agent_path: str | None
    skills: tuple[str, ...]
    skill_paths: tuple[str, ...]
    harness: str | None
    kind: str
    desc: str | None
    work_id: str | None
    harness_session_id: str | None
    execution_cwd: str | None = None
    claude_config_dir: str | None = None
    launch_mode: LaunchMode | None
    worker_pid: int | None
    runner_pid: int | None
    status: SpawnStatus | Literal["unknown"]
    prompt: str | None
    started_at: str | None
    exited_at: str | None
    process_exit_code: int | None
    finished_at: str | None
    exit_code: int | None
    duration_secs: float | None
    total_cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None
    reasoning_tokens: int | None
    cost_is_estimate: bool
    error: str | None
    terminal_origin: SpawnOrigin | None


class FinalizeOutcome(BaseModel):
    """Result of a finalize write with the exact post-write projection.

    ``transitioned`` preserves the legacy "this writer moved an active row to a
    terminal state" signal.  ``wrote`` distinguishes rejected reconciler attempts
    from finalize events that were appended for authoritative metadata/override.
    ``snapshot`` is the store projection immediately after the append, computed
    under the same lock, so callers can emit events without a post-write reread
    race.
    """

    model_config = ConfigDict(frozen=True)

    transitioned: bool
    wrote: bool
    snapshot: SpawnRecord | None


class SpawnStartEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    v: int = 1
    event: Literal["start"] = "start"
    id: str = ""
    chat_id: str | None = None
    parent_id: str | None = None
    model: str | None = None
    agent: str | None = None
    agent_path: str | None = None
    skills: tuple[str, ...] = ()
    skill_paths: tuple[str, ...] = ()
    harness: str | None = None
    kind: str | None = None
    desc: str | None = None
    work_id: str | None = None
    harness_session_id: str | None = None
    execution_cwd: str | None = None
    launch_mode: LaunchMode | None = None
    worker_pid: int | None = None
    runner_pid: int | None = None
    status: SpawnStatus = "running"
    prompt: str | None = None
    started_at: str | None = None


class SpawnUpdateEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    v: int = 1
    event: Literal["update"] = "update"
    id: str = ""
    status: SpawnStatus | None = None
    launch_mode: LaunchMode | None = None
    worker_pid: int | None = None
    runner_pid: int | None = None
    harness_session_id: str | None = None
    execution_cwd: str | None = None
    claude_config_dir: str | None = None
    error: str | None = None
    desc: str | None = None
    work_id: str | None = None


class SpawnExitedEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    v: int = 1
    event: Literal["exited"] = "exited"
    id: str = ""
    exit_code: int = 0
    exited_at: str | None = None


class SpawnFinalizeEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    v: int = 1
    event: Literal["finalize"] = "finalize"
    id: str = ""
    status: SpawnStatus | None = None
    exit_code: int | None = None
    finished_at: str | None = None
    duration_secs: float | None = None
    total_cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_is_estimate: bool = False
    error: str | None = None
    origin: SpawnOrigin | None = None


type SpawnEvent = SpawnStartEvent | SpawnUpdateEvent | SpawnExitedEvent | SpawnFinalizeEvent

def _coerce_launch_mode(value: object) -> object:
    if not isinstance(value, str):
        return value
    normalized = value.strip().lower()
    if normalized in _LAUNCH_MODE_VALUES:
        return normalized
    return value


def _parse_event(payload: dict[str, Any]) -> SpawnEvent | None:
    resolved_payload = payload
    if "launch_mode" in payload:
        coerced_launch_mode = _coerce_launch_mode(payload.get("launch_mode"))
        if coerced_launch_mode != payload.get("launch_mode"):
            resolved_payload = dict(payload)
            resolved_payload["launch_mode"] = coerced_launch_mode

    event_type = resolved_payload.get("event")
    try:
        if event_type == "start":
            return SpawnStartEvent.model_validate(resolved_payload)
        if event_type == "update":
            return SpawnUpdateEvent.model_validate(resolved_payload)
        if event_type == "exited":
            return SpawnExitedEvent.model_validate(resolved_payload)
        if event_type == "finalize":
            return SpawnFinalizeEvent.model_validate(resolved_payload)
    except ValidationError:
        return None
    return None


parse_event = _parse_event


def start_spawn(
    runtime_root: Path,
    *,
    chat_id: str,
    parent_id: str | None = None,
    model: str,
    agent: str,
    agent_path: str | None = None,
    skills: tuple[str, ...] = (),
    skill_paths: tuple[str, ...] = (),
    harness: str,
    kind: str = "child",
    prompt: str,
    desc: str | None = None,
    work_id: str | None = None,
    spawn_id: SpawnId | str | None = None,
    harness_session_id: str | None = None,
    execution_cwd: str | None = None,
    launch_mode: LaunchMode | None = None,
    worker_pid: int | None = None,
    runner_pid: int | None = None,
    status: SpawnStatus = "running",
    started_at: str | None = None,
    clock: Clock | None = None,
    repository: SpawnRepository | None = None,
) -> SpawnId:
    """Append a spawn start event under `spawns.jsonl.flock` and return the spawn ID."""

    resolved_clock = clock or RealClock()
    paths = RuntimePaths.from_root_dir(runtime_root)
    resolved_repository = _resolve_repository(
        paths,
        repository=repository,
    )
    started = started_at or resolved_clock.utc_now_iso()

    with lock_file(paths.spawns_flock):
        if spawn_id is not None:
            resolved_spawn_id = SpawnId(str(spawn_id))
        else:
            current = _read_spawn_counter(paths)
            if current == 0 and not _spawn_counter_path(paths).is_file():
                current = _seed_spawn_counter_from_events(resolved_repository.read_events())
            next_value = current + 1
            atomic_write_text(_spawn_counter_path(paths), f"{next_value}\n")
            resolved_spawn_id = SpawnId(f"p{next_value}")
        event = SpawnStartEvent(
            id=str(resolved_spawn_id),
            chat_id=chat_id,
            parent_id=parent_id,
            model=model,
            agent=agent,
            agent_path=agent_path,
            skills=skills,
            skill_paths=skill_paths,
            harness=harness,
            kind=kind,
            desc=desc,
            work_id=work_id,
            harness_session_id=harness_session_id,
            execution_cwd=execution_cwd,
            launch_mode=launch_mode,
            worker_pid=worker_pid,
            runner_pid=runner_pid,
            status=status,
            started_at=started,
            prompt=prompt,
        )
        resolved_repository.append_event(event)
        return resolved_spawn_id


def remove_spawn_events(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    repository: SpawnRepository | None = None,
) -> None:
    """Remove all events for one spawn from the JSONL store.

    This narrow rollback seam is for failed spawn preparation before any runner
    can observe or act on the row. It is not a user-facing delete/archive path.
    """

    paths = RuntimePaths.from_root_dir(runtime_root)
    resolved_repository = _resolve_repository(paths, repository=repository)
    wanted = str(spawn_id)
    with lock_file(paths.spawns_flock):
        retained = [
            event
            for event in resolved_repository.read_events()
            if getattr(event, "id", "") != wanted
        ]
        lines = [
            event.model_dump_json(exclude_none=True, by_alias=False) + "\n"
            for event in retained
        ]
        atomic_write_text(paths.spawns_jsonl, "".join(lines))


def update_spawn(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    launch_mode: LaunchMode | None = None,
    worker_pid: int | None = None,
    runner_pid: int | None = None,
    harness_session_id: str | None = None,
    execution_cwd: str | None = None,
    claude_config_dir: str | None = None,
    error: str | None = None,
    desc: str | None = None,
    work_id: str | None = None,
    repository: SpawnRepository | None = None,
) -> None:
    """Append a metadata update event under `spawns.jsonl.flock`."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    resolved_repository = _resolve_repository(paths, repository=repository)
    event = SpawnUpdateEvent(
        id=str(spawn_id),
        launch_mode=launch_mode,
        worker_pid=worker_pid,
        runner_pid=runner_pid,
        harness_session_id=harness_session_id,
        execution_cwd=execution_cwd,
        claude_config_dir=claude_config_dir,
        error=error,
        desc=desc,
        work_id=work_id,
    )
    resolved_repository.append_event(event)
    record = get_spawn(runtime_root, spawn_id, repository=resolved_repository)
    if record is not None:
        from meridian.lib.core.telemetry import (
            LifecycleEvent,
            next_spawn_sequence,
            notify_observers,
        )

        notify_observers(
            LifecycleEvent(
                event="spawn.updated",
                spawn_id=str(record.id),
                harness_id=record.harness or "",
                model=record.model or "",
                agent=record.agent,
                ts=datetime.now(tz=UTC),
                seq=next_spawn_sequence(record.id),
                payload={
                    "launch_mode": launch_mode,
                    "worker_pid": worker_pid,
                    "runner_pid": runner_pid,
                    "harness_session_id": harness_session_id,
                    "execution_cwd": execution_cwd,
                    "claude_config_dir": claude_config_dir,
                    "error": error,
                    "desc": desc,
                    "work_id": work_id,
                },
            )
        )


def record_spawn_exited(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    exit_code: int,
    exited_at: str | None = None,
    clock: Clock | None = None,
    repository: SpawnRepository | None = None,
) -> None:
    """Append an exited event — the harness process has exited."""

    resolved_clock = clock or RealClock()
    paths = RuntimePaths.from_root_dir(runtime_root)
    resolved_repository = _resolve_repository(
        paths,
        repository=repository,
    )
    event = SpawnExitedEvent(
        id=str(spawn_id),
        exit_code=exit_code,
        exited_at=exited_at or resolved_clock.utc_now_iso(),
    )
    resolved_repository.append_event(event)


def finalize_spawn(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    status: SpawnStatus,
    exit_code: int,
    *,
    origin: SpawnOrigin,
    duration_secs: float | None = None,
    total_cost_usd: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_input_tokens: int | None = None,
    cache_creation_input_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    cost_is_estimate: bool = False,
    finished_at: str | None = None,
    error: str | None = None,
    clock: Clock | None = None,
    repository: SpawnRepository | None = None,
) -> FinalizeOutcome:
    """Append a finalize event and return write/transition details.

    Always writes the event so metadata is never lost -- the projection
    merges duration, cost, and token counts from every finalize event.
    ``outcome.transitioned`` is True when the spawn was active (queued,
    running, or finalizing) before this call, meaning this writer is the one
    that moved it to a terminal state. It is False when the spawn was already
    terminal or does not exist. Authoritative finalize events may still write
    and replace a reconciler terminal tuple; callers should use
    ``outcome.wrote`` and ``outcome.snapshot`` for post-write event emission.
    """
    resolved_clock = clock or RealClock()
    paths = RuntimePaths.from_root_dir(runtime_root)
    resolved_repository = _resolve_repository(
        paths,
        repository=repository,
    )
    with lock_file(paths.spawns_flock):
        records = reduce_events(resolved_repository.read_events())
        record = records.get(str(spawn_id))
        if origin == "reconciler" and (
            record is None or record.status in _TERMINAL_SPAWN_STATUSES
        ):
            logger.info(
                "Reconciler finalize rejected because row was missing or already terminal.",
                spawn_id=str(spawn_id),
                current_status=record.status if record is not None else None,
                attempted_status=status,
                attempted_error=error,
            )
            return FinalizeOutcome(transitioned=False, wrote=False, snapshot=record)
        if (
            record is not None
            and record.status != "unknown"
            and record.status not in _TERMINAL_SPAWN_STATUSES
        ):
            _validate_transition(record.status, status)
        was_active = record is not None and is_active_spawn_status(record.status)
        event = SpawnFinalizeEvent(
            id=str(spawn_id),
            status=status,
            exit_code=exit_code,
            finished_at=finished_at or resolved_clock.utc_now_iso(),
            duration_secs=duration_secs,
            total_cost_usd=total_cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            reasoning_tokens=reasoning_tokens,
            cost_is_estimate=cost_is_estimate,
            error=error,
            origin=origin,
        )
        resolved_repository.append_event(event)
        snapshot = reduce_events(resolved_repository.read_events()).get(str(spawn_id))
        return FinalizeOutcome(
            transitioned=was_active,
            wrote=True,
            snapshot=snapshot,
        )


def mark_finalizing(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    repository: SpawnRepository | None = None,
) -> bool:
    """CAS transition `running -> finalizing` under the spawn-store flock."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    resolved_repository = _resolve_repository(paths, repository=repository)
    with lock_file(paths.spawns_flock):
        records = reduce_events(resolved_repository.read_events())
        record = records.get(str(spawn_id))
        if record is None or record.status != "running":
            return False
        _validate_transition(record.status, "finalizing")
        event = SpawnUpdateEvent(id=str(spawn_id), status="finalizing")
        resolved_repository.append_event(event)
        return True


def mark_spawn_running(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    launch_mode: LaunchMode | None = None,
    worker_pid: int | None = None,
    runner_pid: int | None = None,
    repository: SpawnRepository | None = None,
) -> bool:
    paths = RuntimePaths.from_root_dir(runtime_root)
    resolved_repository = _resolve_repository(paths, repository=repository)
    with lock_file(paths.spawns_flock):
        records = reduce_events(resolved_repository.read_events())
        record = records.get(str(spawn_id))
        if record is None:
            return False
        changed = record.status != "running"
        if record.status not in {"unknown", "running"}:
            _validate_transition(cast("SpawnStatus", record.status), "running")
        event = SpawnUpdateEvent(
            id=str(spawn_id),
            status="running",
            launch_mode=launch_mode,
            worker_pid=worker_pid,
            runner_pid=runner_pid,
        )
        resolved_repository.append_event(event)
        return changed


def _spawn_sort_key(spawn: SpawnRecord) -> tuple[int, str]:
    if len(spawn.id) >= 2 and spawn.id[0] in {"p", "r"} and spawn.id[1:].isdigit():
        return (int(spawn.id[1:]), spawn.id)
    return (10**9, spawn.id)


def list_spawns(
    runtime_root: Path,
    filters: Mapping[str, Any] | None = None,
    *,
    repository: SpawnRepository | None = None,
) -> list[SpawnRecord]:
    """List derived spawn records with optional equality filters."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    resolved_repository = _resolve_repository(paths, repository=repository)
    spawns = list(reduce_events(resolved_repository.read_events()).values())

    if filters:
        filtered: list[SpawnRecord] = []
        for spawn in spawns:
            spawn_data = spawn.model_dump()
            keep = True
            for key, expected in filters.items():
                if expected is None:
                    continue
                if key not in spawn_data:
                    continue
                if spawn_data[key] != expected:
                    keep = False
                    break
            if keep:
                filtered.append(spawn)
        spawns = filtered

    return sorted(spawns, key=_spawn_sort_key)


def get_spawn(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    repository: SpawnRepository | None = None,
) -> SpawnRecord | None:
    """Return one spawn by ID.

    Reads the full event log and reduces it to find the requested spawn.
    Uses ``reduce_events`` directly instead of ``list_spawns`` to avoid
    sorting overhead; the result dict is keyed by spawn ID so the lookup
    is O(1) after the single event-log parse.
    """

    paths = RuntimePaths.from_root_dir(runtime_root)
    resolved_repository = _resolve_repository(paths, repository=repository)
    records = reduce_events(resolved_repository.read_events())
    return records.get(str(spawn_id))


def spawn_stats(
    runtime_root: Path,
    *,
    repository: SpawnRepository | None = None,
) -> dict[str, Any]:
    """Aggregate high-level spawn stats from JSONL-derived records."""

    spawns = list_spawns(runtime_root, repository=repository)
    by_status: dict[str, int] = {}
    by_model: dict[str, int] = {}
    total_duration_secs = 0.0
    total_cost_usd = 0.0
    total_input_tokens = 0
    total_output_tokens = 0

    for spawn in spawns:
        by_status[spawn.status] = by_status.get(spawn.status, 0) + 1
        if spawn.model is not None:
            by_model[spawn.model] = by_model.get(spawn.model, 0) + 1
        if spawn.duration_secs is not None:
            total_duration_secs += spawn.duration_secs
        if spawn.total_cost_usd is not None:
            total_cost_usd += spawn.total_cost_usd
        if spawn.input_tokens is not None:
            total_input_tokens += spawn.input_tokens
        if spawn.output_tokens is not None:
            total_output_tokens += spawn.output_tokens

    return {
        "total_runs": len(spawns),
        "by_status": by_status,
        "by_model": by_model,
        "total_duration_secs": total_duration_secs,
        "total_cost_usd": total_cost_usd,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
    }
