"""Durable process-scope state projection.

Persists process scope snapshots as part of spawn lifecycle state.
All writes are atomic (tmp+rename). All reads tolerate missing or corrupt data.

The sidecar file at ``<runtime_root>/spawns/<spawn_id>/process_scopes.json``
stores scope metadata only — it is NOT authoritative for spawn state.  It is
auxiliary metadata for cleanup (reaper, stop) only.  The data can be folded
into the main spawn projection in a future phase.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Annotated, NotRequired

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    TypeAdapter,
    ValidationError,
    with_config,
)
from typing_extensions import TypedDict

from meridian.lib.core.refs import parse_ref
from meridian.lib.core.spawn_lifecycle import is_terminal_spawn_status
from meridian.lib.core.types import SpawnId
from meridian.lib.platform.locking import lock_file
from meridian.lib.platform.process_scope import ProcessScopeSnapshot
from meridian.lib.state.history_changes import HistoryChanges
from meridian.lib.state.json_doc import mutate_model, read_model
from meridian.lib.state.spawn.repository import read_state, spawn_lock_path
from meridian.lib.utils.json_shapes import json_object, tuple_or_empty

logger = logging.getLogger(__name__)

_SIDECAR_FILENAME = "process_scopes.json"
_CLEANUP_CLAIM_FILENAME = "reaper_cleanup_claim.json"
_FIRST_WRITER_WAIT_SECONDS = 2.0


type _TolerantTuple = Annotated[tuple[object, ...], BeforeValidator(tuple_or_empty)]


class ScopeProjection(BaseModel):
    """Versionless process-scope sidecar document."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    scopes: _TolerantTuple = ()
    released: _TolerantTuple = ()


@with_config(ConfigDict(strict=True, extra="forbid"))
class ScopeRow(TypedDict):
    scope_id: str
    owner_policy: str
    owner_id: str
    role: str
    containment: str
    root_pid: int
    root_created_at_epoch: float
    pgid: int | None
    job_name: str | None
    degraded_reason: str | None
    parent_death_linked: NotRequired[bool]
    release_id: NotRequired[str]


_SCOPE_ROW_ADAPTER = TypeAdapter(ScopeRow)


@dataclasses.dataclass(frozen=True)
class ScopeProjectionSnapshot:
    """Immutable typed view of one scope projection read under one lock."""

    scopes: tuple[ProcessScopeSnapshot, ...]
    released_ids: frozenset[str]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sidecar_path(runtime_root: Path, spawn_id: SpawnId) -> Path:
    return runtime_root / "spawns" / str(spawn_id) / _SIDECAR_FILENAME


def _read_raw(path: Path) -> ScopeProjection:
    """Read sidecar JSON; return default payload on any error."""
    return read_model(path, ScopeProjection) or ScopeProjection()


def scope_projection_lock_path(runtime_root: Path, spawn_id: SpawnId | str) -> Path:
    """Return the stable lock for one scope sidecar."""
    return runtime_root / "locks" / "process-scopes" / f"{spawn_id}.lock"


def _mutate_scope_projection(
    runtime_root: Path,
    spawn_id: SpawnId,
    mutator: Callable[[ScopeProjection], ScopeProjection],
) -> ScopeProjection:
    """Apply one sidecar RMW under its stable per-spawn lock."""
    path = _sidecar_path(runtime_root, spawn_id)
    updated, _ = mutate_model(
        path,
        ScopeProjection,
        lambda current: (mutator(current or ScopeProjection()), None),
        lock=lock_file(
            scope_projection_lock_path(runtime_root, spawn_id),
            reentrant=False,
        ),
    )
    return updated or ScopeProjection()


def scope_snapshot_from_dict(data: object) -> ProcessScopeSnapshot | None:
    """Deserialize one scope dict; return None on any error."""
    try:
        row = _SCOPE_ROW_ADAPTER.validate_python(data)
        return ProcessScopeSnapshot(**row)
    except (TypeError, ValidationError):
        logger.debug("process_scope_projection: failed to deserialize scope: %r", data)
        return None


def parse_scope_entries(entries: Iterable[object]) -> list[ProcessScopeSnapshot]:
    """Deserialize the valid process scopes in one persisted entry list."""
    scopes: list[ProcessScopeSnapshot] = []
    for entry in entries:
        snapshot = scope_snapshot_from_dict(entry)
        if snapshot is not None:
            scopes.append(snapshot)
    return scopes


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_scope(
    runtime_root: Path,
    spawn_id: SpawnId,
    snapshot: ProcessScopeSnapshot,
) -> None:
    """Persist a process scope snapshot for a spawn.

    Reads existing sidecar (or starts fresh), upserts the concrete scope release,
    and writes atomically.  Re-recording the same process scope (same scope id,
    PID, and birth time) replaces the previous projection so provisional launch
    ownership can be upgraded without leaving duplicate cleanup targets. Distinct
    concrete releases with the same human label are preserved.
    """
    spawns_dir = runtime_root / "spawns"
    # Global order when both locks are needed: spawn state, then scope projection.
    # This makes cleanup-claim snapshots and registration mutually exclusive.
    with (
        lock_file(HistoryChanges(runtime_root).mutation_lock, mode="shared"),
        lock_file(
            spawn_lock_path(spawns_dir, parse_ref(str(spawn_id), row_key=True)), reentrant=False
        ),
    ):
        current = read_state(
            spawns_dir, parse_ref(str(spawn_id), row_key=True), include_prompt=False
        )
        claim_path = spawns_dir / str(spawn_id) / _CLEANUP_CLAIM_FILENAME
        if current is None:
            raise ValueError(
                f"Refusing process-scope registration: spawn does not exist: {spawn_id}"
            )
        if (
            current.record_mode == "historical"
            or is_terminal_spawn_status(current.status)
            or claim_path.exists()
        ):
            raise ValueError(f"Refusing process-scope registration after cleanup began: {spawn_id}")

        def upsert(payload: ScopeProjection) -> ScopeProjection:
            snapshot_dict = dataclasses.asdict(snapshot)
            scopes: list[object] = []
            replaced = False
            for entry in payload.scopes:
                existing = json_object(entry)
                existing_release_id = existing.get("release_id") if existing is not None else None
                if existing_release_id == snapshot.release_id:
                    if not replaced:
                        scopes.append(snapshot_dict)
                        replaced = True
                    continue
                scopes.append(entry)
            if not replaced:
                scopes.append(snapshot_dict)
            return payload.model_copy(update={"scopes": tuple(scopes)})

        _mutate_scope_projection(runtime_root, spawn_id, upsert)


def mark_scope_released(
    runtime_root: Path,
    spawn_id: SpawnId,
    release_id: str,
) -> None:
    """Mark a scope as released (terminated / cleaned up).

    Prevents double-cleanup when the reaper runs again after process exit.
    Idempotent — safe to call multiple times for the same release_id.
    Does nothing after the published spawn has been deleted.
    """

    def mark_released(payload: ScopeProjection) -> ScopeProjection:
        if release_id in payload.released:
            return payload
        return payload.model_copy(update={"released": (*payload.released, release_id)})

    spawns_dir = runtime_root / "spawns"
    # Global order when both locks are needed: spawn state, then scope projection.
    with (
        lock_file(HistoryChanges(runtime_root).mutation_lock, mode="shared"),
        lock_file(
            spawn_lock_path(spawns_dir, parse_ref(str(spawn_id), row_key=True)), reentrant=False
        ),
    ):
        current = read_state(
            spawns_dir, parse_ref(str(spawn_id), row_key=True), include_prompt=False
        )
        if current is None or current.record_mode == "historical":
            return
        _mutate_scope_projection(runtime_root, spawn_id, mark_released)


def read_scope_projection(
    runtime_root: Path,
    spawn_id: SpawnId,
) -> ScopeProjectionSnapshot:
    """Read scopes and release markers from one projection snapshot.

    A lock path without a sidecar can mean the first writer is publishing. Wait
    briefly for that normal case, but do not let a wedged writer make cleanup
    reads block forever. Atomic replacement makes the timeout fallback safe to
    read without the lock: it observes either no sidecar or one complete
    snapshot.
    """

    path = _sidecar_path(runtime_root, spawn_id)
    lock_path = scope_projection_lock_path(runtime_root, spawn_id)
    if not path.is_file() and not lock_path.exists():
        return ScopeProjectionSnapshot(scopes=(), released_ids=frozenset())

    try:
        with lock_file(
            lock_path,
            timeout=_FIRST_WRITER_WAIT_SECONDS,
            reentrant=False,
        ):
            payload = _read_raw(path)
    except TimeoutError:
        payload = _read_raw(path)

    scopes = parse_scope_entries(payload.scopes)
    released_ids = frozenset(entry for entry in payload.released if isinstance(entry, str))
    return ScopeProjectionSnapshot(scopes=tuple(scopes), released_ids=released_ids)


def is_scope_released(
    runtime_root: Path,
    spawn_id: SpawnId,
    release_id: str,
) -> bool:
    """Check if a scope has been marked as released.

    Returns False on any read error so callers fail open (attempt cleanup).
    """
    return release_id in read_scope_projection(runtime_root, spawn_id).released_ids


def read_scopes_from_disk(
    runtime_root: Path,
    spawn_id: SpawnId,
) -> list[ProcessScopeSnapshot]:
    """Read scope snapshots directly from disk for a spawn.

    Tolerates missing, truncated, or corrupt files — returns ``[]`` on any
    error.  Use when the spawn record may not have been refreshed yet (e.g.
    immediately after ``record_scope`` in the same process).
    """
    return list(read_scope_projection(runtime_root, spawn_id).scopes)


__all__ = [
    "ScopeProjectionSnapshot",
    "is_scope_released",
    "mark_scope_released",
    "parse_scope_entries",
    "read_scope_projection",
    "read_scopes_from_disk",
    "record_scope",
    "scope_projection_lock_path",
    "scope_snapshot_from_dict",
]
