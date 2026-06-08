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
from pathlib import Path
from typing import cast

from meridian.lib.core.types import SpawnId
from meridian.lib.platform.process_scope import ProcessScopeSnapshot
from meridian.lib.platform.process_scope.base import process_scope_release_id
from meridian.lib.state.atomic import atomic_write_text

logger = logging.getLogger(__name__)

_SIDECAR_FILENAME = "process_scopes.json"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sidecar_path(runtime_root: Path, spawn_id: SpawnId) -> Path:
    return runtime_root / "spawns" / str(spawn_id) / _SIDECAR_FILENAME


def _default_payload() -> dict[str, object]:
    return {"scopes": [], "released": []}


def _read_raw(path: Path) -> dict[str, object]:
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
        return raw
    except Exception:
        return _default_payload()


def _snapshot_to_dict(snapshot: ProcessScopeSnapshot) -> dict[str, object]:
    return dataclasses.asdict(snapshot)


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

    Reads existing sidecar (or starts fresh), appends the new scope entry,
    and writes atomically.  Safe to call concurrently — last-write-wins per
    scope_id since each call reads before writing.
    """
    path = _sidecar_path(runtime_root, spawn_id)
    payload = _read_raw(path)
    scopes: list[object] = list(payload["scopes"])  # type: ignore[arg-type]
    scopes.append(_snapshot_to_dict(snapshot))
    payload["scopes"] = scopes
    atomic_write_text(path, json.dumps(payload, separators=(",", ":")))


def mark_scope_released(
    runtime_root: Path,
    spawn_id: SpawnId,
    release_id: str,
) -> None:
    """Mark a scope as released (terminated / cleaned up).

    Prevents double-cleanup when the reaper runs again after process exit.
    Idempotent — safe to call multiple times for the same release_id.
    """
    path = _sidecar_path(runtime_root, spawn_id)
    payload = _read_raw(path)
    released: list[object] = list(payload["released"])  # type: ignore[arg-type]
    if release_id not in released:
        released.append(release_id)
    payload["released"] = released
    atomic_write_text(path, json.dumps(payload, separators=(",", ":")))


def is_scope_released(
    runtime_root: Path,
    spawn_id: SpawnId,
    release_id: str,
) -> bool:
    """Check if a scope has been marked as released.

    Returns False on any read error so callers fail open (attempt cleanup).
    """
    path = _sidecar_path(runtime_root, spawn_id)
    if not path.exists():
        return False
    payload = _read_raw(path)
    released = payload.get("released", [])
    if not isinstance(released, list):
        return False
    return release_id in released


def read_scopes_from_disk(
    runtime_root: Path,
    spawn_id: SpawnId,
) -> list[ProcessScopeSnapshot]:
    """Read scope snapshots directly from disk for a spawn.

    Tolerates missing, truncated, or corrupt files — returns ``[]`` on any
    error.  Use when the spawn record may not have been refreshed yet (e.g.
    immediately after ``record_scope`` in the same process).
    """
    path = _sidecar_path(runtime_root, spawn_id)
    if not path.exists():
        return []
    payload = _read_raw(path)
    snapshots: list[ProcessScopeSnapshot] = []
    for entry in cast("list[object]", payload["scopes"]):
        if not isinstance(entry, dict):
            continue
        snap = scope_snapshot_from_dict(cast("dict[str, object]", entry))
        if snap is not None:
            snapshots.append(snap)
    return snapshots


__all__ = [
    "is_scope_released",
    "mark_scope_released",
    "read_scopes_from_disk",
    "record_scope",
    "scope_snapshot_from_dict",
]
