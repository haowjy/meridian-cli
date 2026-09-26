"""One-time rewrite of spawn rows written by the PR-1 dogfood build.

That build persisted the run boundary as flat ``entry_chat_id`` / ``exit_chat_id``
/ ``exit_identity`` / ``trampoline_successor_id`` fields. The strict v3 schema
rejects them, so such rows quarantine until this migration rewrites them.
Remove this module once no dogfood rows remain on user machines.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.spawn.model import RunBoundaryOutcome
from meridian.lib.state.spawn.repository import (
    DOGFOOD_BOUNDARY_FIELDS,
    StoredSpawnState,
    scan_spawn_ids,
    spawn_lock_path,
)

_DOGFOOD_MARKERS = tuple(f'"{name}"'.encode() for name in DOGFOOD_BOUNDARY_FIELDS)


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


@dataclass(frozen=True)
class DogfoodMigration:
    migrated: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]
    """``(spawn_id, reason)`` for rows that still quarantine after this pass."""


def _migrate_row(changes: HistoryChanges, spawns_dir: Path, spawn_id: str) -> bool:
    path = spawns_dir / spawn_id / "state.json"
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return False
    if not any(marker in content for marker in _DOGFOOD_MARKERS):
        return False
    with (
        lock_file(changes.mutation_lock, mode="shared"),
        lock_file(spawn_lock_path(spawns_dir, spawn_id), reentrant=False),
    ):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not raw.keys() & set(DOGFOOD_BOUNDARY_FIELDS):
            return False
        stored = StoredSpawnState.model_validate(_translate(raw))
        changes.mark(HistorySource(kind="spawn", key=spawn_id))
        atomic_write_text(path, stored.model_dump_json(indent=2) + "\n")
    return True


def migrate_dogfood_spawn_rows(runtime_root: Path) -> DogfoodMigration:
    """Rewrite dogfood-shaped ``state.json`` rows in place; idempotent.

    Each row is isolated: a malformed row is reported in ``failed`` and the
    pass moves on, so one bad row never keeps the rest quarantined.
    """

    spawns_dir = runtime_root / "spawns"
    changes = HistoryChanges(runtime_root)
    migrated: list[str] = []
    failed: list[tuple[str, str]] = []
    for spawn_id in scan_spawn_ids(spawns_dir):
        try:
            if _migrate_row(changes, spawns_dir, spawn_id):
                migrated.append(spawn_id)
        except Exception as exc:
            failed.append((spawn_id, _reason(exc)))
    return DogfoodMigration(migrated=tuple(migrated), failed=tuple(failed))


def _reason(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        detail = "; ".join(
            f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in exc.errors()
        )
    else:
        detail = str(exc).splitlines()[0] if str(exc) else ""
    return f"{type(exc).__name__}: {detail}"


__all__ = ["DogfoodMigration", "migrate_dogfood_spawn_rows"]
