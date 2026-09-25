"""One-time rewrite of spawn rows written by the PR-1 dogfood build.

That build persisted the run boundary as flat ``entry_chat_id`` / ``exit_chat_id``
/ ``exit_identity`` / ``trampoline_successor_id`` fields. The strict v3 schema
rejects them, so such rows quarantine until this migration rewrites them.
Remove this module once no dogfood rows remain on user machines.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.spawn.model import RunBoundaryOutcome
from meridian.lib.state.spawn.repository import (
    StoredSpawnState,
    scan_spawn_ids,
    spawn_lock_path,
)

_DOGFOOD_FIELDS = ("entry_chat_id", "exit_chat_id", "exit_identity", "trampoline_successor_id")
_DOGFOOD_MARKERS = tuple(f'"{name}"'.encode() for name in _DOGFOOD_FIELDS)


def _translate(raw: dict[str, Any]) -> dict[str, Any]:
    data = dict(raw)
    entry_chat_id = data.pop("entry_chat_id", None)
    exit_chat_id = data.pop("exit_chat_id", None)
    exit_identity = data.pop("exit_identity", None)
    trampoline = data.pop("trampoline_successor_id", None)
    if data.get("chat_id") is None and entry_chat_id is not None:
        data["chat_id"] = entry_chat_id
    if data.get("run_boundary") is None and exit_identity is not None:
        data["run_boundary"] = {"status": exit_identity, "exit_chat_id": exit_chat_id}
    if trampoline is not None:
        boundary = RunBoundaryOutcome.model_validate(
            data.get("run_boundary") or {"status": "unresolved"}
        ).model_dump()
        data["run_boundary"] = {**boundary, "trampoline_successor_id": trampoline}
    return data


def migrate_dogfood_spawn_rows(runtime_root: Path) -> tuple[str, ...]:
    """Rewrite dogfood-shaped ``state.json`` rows in place; idempotent."""

    spawns_dir = runtime_root / "spawns"
    changes = HistoryChanges(runtime_root)
    migrated: list[str] = []
    for spawn_id in scan_spawn_ids(spawns_dir):
        path = spawns_dir / spawn_id / "state.json"
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            continue
        if not any(marker in content for marker in _DOGFOOD_MARKERS):
            continue
        with (
            lock_file(changes.mutation_lock, mode="shared"),
            lock_file(spawn_lock_path(spawns_dir, spawn_id), reentrant=False),
        ):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not raw.keys() & set(_DOGFOOD_FIELDS):
                continue
            stored = StoredSpawnState.model_validate(_translate(raw))
            changes.mark(HistorySource(kind="spawn", key=spawn_id))
            atomic_write_text(path, stored.model_dump_json(indent=2) + "\n")
        migrated.append(spawn_id)
    return tuple(migrated)


__all__ = ["migrate_dogfood_spawn_rows"]
