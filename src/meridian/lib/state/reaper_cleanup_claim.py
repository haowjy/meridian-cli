"""Durable cleanup intent for finalize-first orphan reconciliation."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import cast

from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.core.types import SpawnId
from meridian.lib.platform.atomic import fsync_directory
from meridian.lib.platform.locking import lock_file
from meridian.lib.platform.process_scope.base import ProcessScopeSnapshot
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.process_scope_projection import (
    is_scope_released,
    read_scopes_from_disk,
    scope_snapshot_from_dict,
)
from meridian.lib.state.spawn.repository import read_state, spawn_lock_path

_CLAIM_FILENAME = "reaper_cleanup_claim.json"
_CLAIM_VERSION = 1


def cleanup_claim_path(runtime_root: Path, spawn_id: SpawnId | str) -> Path:
    return runtime_root / "spawns" / str(spawn_id) / _CLAIM_FILENAME


def cleanup_lock_path(runtime_root: Path, spawn_id: SpawnId | str) -> Path:
    return runtime_root / "locks" / "reaper-cleanup" / f"{spawn_id}.lock"


def read_cleanup_claim(
    runtime_root: Path,
    spawn_id: SpawnId | str,
) -> list[ProcessScopeSnapshot]:
    path = cleanup_claim_path(runtime_root, spawn_id)
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    payload = cast("dict[str, object]", raw)
    if payload.get("v") != _CLAIM_VERSION:
        return []
    entries = payload.get("scopes")
    if not isinstance(entries, list):
        return []
    scopes: list[ProcessScopeSnapshot] = []
    for entry in cast("list[object]", entries):
        if not isinstance(entry, dict):
            continue
        entry_payload = cast("dict[str, object]", entry)
        scope = scope_snapshot_from_dict(dict(entry_payload))
        if scope is not None:
            scopes.append(scope)
    return scopes


def _write_claim(path: Path, scopes: list[ProcessScopeSnapshot]) -> None:
    payload = {
        "v": _CLAIM_VERSION,
        "scopes": [dataclasses.asdict(scope) for scope in scopes],
    }
    atomic_write_text(path, json.dumps(payload, separators=(",", ":")) + "\n")


def _delete_claim(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    fsync_directory(path.parent)


def claim_active_spawn_scopes(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    extra_scopes: tuple[ProcessScopeSnapshot, ...] = (),
) -> list[ProcessScopeSnapshot]:
    """Snapshot cleanup targets while coordinating with the spawn terminal CAS."""
    spawn_id_text = str(spawn_id)
    spawns_dir = runtime_root / "spawns"
    with lock_file(spawn_lock_path(spawns_dir, spawn_id_text), reentrant=False):
        current = read_state(spawns_dir, spawn_id_text)
        if current is None or not is_active_spawn_status(current.status):
            return read_cleanup_claim(runtime_root, spawn_id)
        existing = read_cleanup_claim(runtime_root, spawn_id)
        candidates = [
            scope
            for scope in read_scopes_from_disk(runtime_root, SpawnId(spawn_id_text))
            if scope.owner_policy == "spawn_owned"
            and not is_scope_released(runtime_root, SpawnId(spawn_id_text), scope.release_id)
        ]
        by_release_id = {
            scope.release_id: scope for scope in (*existing, *candidates, *extra_scopes)
        }
        claimed = list(by_release_id.values())
        path = cleanup_claim_path(runtime_root, spawn_id)
        if claimed:
            _write_claim(path, claimed)
        else:
            _delete_claim(path)
        return claimed


def replace_cleanup_claim(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    scopes: list[ProcessScopeSnapshot],
) -> None:
    """Replace or clear a claim under the spawn's stable mutation lock."""
    spawns_dir = runtime_root / "spawns"
    with lock_file(spawn_lock_path(spawns_dir, str(spawn_id)), reentrant=False):
        path = cleanup_claim_path(runtime_root, spawn_id)
        if scopes:
            _write_claim(path, scopes)
        else:
            _delete_claim(path)


__all__ = [
    "claim_active_spawn_scopes",
    "cleanup_claim_path",
    "cleanup_lock_path",
    "read_cleanup_claim",
    "replace_cleanup_claim",
]
