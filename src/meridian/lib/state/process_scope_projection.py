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
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict, cast

from meridian.lib.core.spawn_lifecycle import is_terminal_spawn_status
from meridian.lib.core.types import SpawnId
from meridian.lib.platform.locking import lock_file
from meridian.lib.platform.process_scope import ProcessScopeSnapshot
from meridian.lib.platform.process_scope.base import process_scope_release_id
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.spawn.repository import read_state, spawn_lock_path

logger = logging.getLogger(__name__)

_SIDECAR_FILENAME = "process_scopes.json"
_CLEANUP_CLAIM_FILENAME = "reaper_cleanup_claim.json"


class ScopeProjection(TypedDict):
    scopes: list[object]
    released: list[object]


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


def _default_payload() -> ScopeProjection:
    return {"scopes": [], "released": []}


def _read_raw(path: Path) -> ScopeProjection:
    """Read sidecar JSON; return default payload on any error."""
    try:
        text = path.read_text(encoding="utf-8")
        parsed: object = json.loads(text)
        if not isinstance(parsed, dict):
            return _default_payload()
        raw = cast("dict[str, object]", parsed)
        # Ensure expected keys exist with correct types
        if not isinstance(raw.get("scopes"), list):
            raw["scopes"] = []
        if not isinstance(raw.get("released"), list):
            raw["released"] = []
        return cast("ScopeProjection", raw)
    except Exception:
        return _default_payload()


def _snapshot_to_dict(snapshot: ProcessScopeSnapshot) -> dict[str, object]:
    return dataclasses.asdict(snapshot)


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
    with lock_file(scope_projection_lock_path(runtime_root, spawn_id), reentrant=False):
        updated = mutator(_read_raw(path))
        atomic_write_text(path, json.dumps(updated, separators=(",", ":")))
        return updated


def scope_snapshot_from_dict(data: dict[str, object]) -> ProcessScopeSnapshot | None:
    """Deserialize one scope dict; return None on any error."""
    try:
        parent_death_linked = data.get("parent_death_linked", False)
        if not isinstance(parent_death_linked, bool):
            return None
        data["parent_death_linked"] = parent_death_linked
        if not isinstance(data.get("release_id"), str) or not str(data.get("release_id")):
            scope_id = data.get("scope_id")
            root_pid = data.get("root_pid")
            root_created_at_epoch = data.get("root_created_at_epoch")
            if not isinstance(scope_id, str):
                return None
            if not isinstance(root_pid, int):
                return None
            if not isinstance(root_created_at_epoch, int | float):
                return None
            data["release_id"] = process_scope_release_id(
                scope_id=scope_id,
                root_pid=root_pid,
                root_created_at_epoch=float(root_created_at_epoch),
            )
        return ProcessScopeSnapshot(**data)  # type: ignore[arg-type]
    except Exception:
        logger.debug("process_scope_projection: failed to deserialize scope: %r", data)
        return None


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
    with lock_file(spawn_lock_path(spawns_dir, str(spawn_id)), reentrant=False):
        current = read_state(spawns_dir, str(spawn_id), include_prompt=False)
        claim_path = spawns_dir / str(spawn_id) / _CLEANUP_CLAIM_FILENAME
        if current is None:
            raise ValueError(
                f"Refusing process-scope registration: spawn does not exist: {spawn_id}"
            )
        if is_terminal_spawn_status(current.status) or claim_path.exists():
            raise ValueError(
                f"Refusing process-scope registration after cleanup began: {spawn_id}"
            )

        def upsert(payload: ScopeProjection) -> ScopeProjection:
            snapshot_dict = _snapshot_to_dict(snapshot)
            scopes: list[object] = []
            replaced = False
            for entry in payload["scopes"]:
                existing = (
                    cast("dict[str, object]", entry) if isinstance(entry, dict) else None
                )
                existing_release_id = (
                    existing.get("release_id") if existing is not None else None
                )
                if existing_release_id == snapshot.release_id:
                    if not replaced:
                        scopes.append(snapshot_dict)
                        replaced = True
                    continue
                scopes.append(cast("object", entry))
            if not replaced:
                scopes.append(snapshot_dict)
            payload["scopes"] = scopes
            return payload

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
        if release_id not in payload["released"]:
            payload["released"].append(release_id)
        return payload

    spawns_dir = runtime_root / "spawns"
    # Global order when both locks are needed: spawn state, then scope projection.
    with lock_file(spawn_lock_path(spawns_dir, str(spawn_id)), reentrant=False):
        if read_state(spawns_dir, str(spawn_id), include_prompt=False) is None:
            return
        _mutate_scope_projection(runtime_root, spawn_id, mark_released)


def read_scope_projection(
    runtime_root: Path,
    spawn_id: SpawnId,
) -> ScopeProjectionSnapshot:
    """Read scopes and release markers together under one lock acquisition."""

    path = _sidecar_path(runtime_root, spawn_id)
    with lock_file(scope_projection_lock_path(runtime_root, spawn_id), reentrant=False):
        payload = _read_raw(path)

    scopes: list[ProcessScopeSnapshot] = []
    for entry in payload["scopes"]:
        if not isinstance(entry, dict):
            continue
        snapshot = scope_snapshot_from_dict(cast("dict[str, object]", entry))
        if snapshot is not None:
            scopes.append(snapshot)
    released_ids = frozenset(
        entry for entry in payload["released"] if isinstance(entry, str)
    )
    return ScopeProjectionSnapshot(scopes=tuple(scopes), released_ids=released_ids)


def is_scope_released(
    runtime_root: Path,
    spawn_id: SpawnId,
    release_id: str,
) -> bool:
    """Check if a scope has been marked as released.

    Returns False on any read error so callers fail open (attempt cleanup).
    """
    if not _sidecar_path(runtime_root, spawn_id).is_file():
        return False
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
    if not _sidecar_path(runtime_root, spawn_id).is_file():
        return []
    return list(read_scope_projection(runtime_root, spawn_id).scopes)


__all__ = [
    "ScopeProjectionSnapshot",
    "is_scope_released",
    "mark_scope_released",
    "read_scope_projection",
    "read_scopes_from_disk",
    "record_scope",
    "scope_projection_lock_path",
    "scope_snapshot_from_dict",
]
