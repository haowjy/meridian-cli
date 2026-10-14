"""Durable cleanup intent for finalize-first orphan reconciliation."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.refs import parse_ref
from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.core.types import SpawnId
from meridian.lib.platform.atomic import fsync_directory
from meridian.lib.platform.locking import lock_file
from meridian.lib.platform.process_scope.base import ProcessScopeSnapshot
from meridian.lib.state.history_changes import HistoryChanges
from meridian.lib.state.json_doc import read_model, write_model
from meridian.lib.state.process_scope_projection import (
    parse_scope_entries,
    read_scope_projection,
)
from meridian.lib.state.spawn.repository import read_state, spawn_lock_path

_CLAIM_FILENAME = "reaper_cleanup_claim.json"
_CLAIM_VERSION = 1


class CleanupClaim(BaseModel):
    """Versioned cleanup targets fixed before terminal publication."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    v: Literal[1] = 1
    scopes: tuple[object, ...]


def cleanup_claim_path(runtime_root: Path, spawn_id: SpawnId | str) -> Path:
    return runtime_root / "spawns" / str(spawn_id) / _CLAIM_FILENAME


def cleanup_lock_path(runtime_root: Path, spawn_id: SpawnId | str) -> Path:
    return runtime_root / "locks" / "reaper-cleanup" / f"{spawn_id}.lock"


def read_cleanup_claim(
    runtime_root: Path,
    spawn_id: SpawnId | str,
) -> list[ProcessScopeSnapshot]:
    claim = read_model(cleanup_claim_path(runtime_root, spawn_id), CleanupClaim)
    return parse_scope_entries(claim.scopes) if claim is not None else []


def _write_claim(path: Path, scopes: list[ProcessScopeSnapshot]) -> None:
    write_model(
        path,
        CleanupClaim(scopes=tuple(dataclasses.asdict(scope) for scope in scopes)),
    )


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
    with (
        lock_file(HistoryChanges(runtime_root).mutation_lock, mode="shared"),
        lock_file(
            spawn_lock_path(spawns_dir, parse_ref(spawn_id_text, row_key=True)), reentrant=False
        ),
    ):
        current = read_state(spawns_dir, parse_ref(spawn_id_text, row_key=True))
        if (
            current is None
            or current.record_mode == "historical"
            or not is_active_spawn_status(current.status)
        ):
            return read_cleanup_claim(runtime_root, spawn_id)
        existing = read_cleanup_claim(runtime_root, spawn_id)
        projection = read_scope_projection(runtime_root, SpawnId(spawn_id_text))
        candidates = [
            scope
            for scope in projection.scopes
            if scope.owner_policy == "spawn_owned"
            and scope.release_id not in projection.released_ids
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
