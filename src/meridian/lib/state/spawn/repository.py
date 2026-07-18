"""Spawn v3 state-file persistence helpers.

The helpers in this module persist one ``state.json`` per spawn under the
runtime spawns directory.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import ValidationError, model_validator

from meridian.lib.core.domain import TERMINAL_SPAWN_STATUSES
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.spawn.legacy import (
    LegacySpawnStateUpgradeError,
    upgrade_legacy_spawn_state,
)
from meridian.lib.state.spawn.model import SpawnRecord, SpawnStateFields


class StoredSpawnState(SpawnStateFields):
    """On-disk v3 ``state.json`` representation.

    The prompt body is stored separately in ``starting-prompt.md``; this model
    keeps only ``prompt_length`` metadata so state reads can stay lightweight.
    """

    v: Literal[3]
    prompt_length: int | None = None

    @model_validator(mode="after")
    def require_terminal_facts(self) -> Self:
        """Keep terminal status and its required facts one atomic persisted meaning."""

        if (self.status in TERMINAL_SPAWN_STATUSES) != (self.terminal is not None):
            raise ValueError("terminal status and terminal facts must appear together")
        return self


@dataclass(frozen=True)
class SpawnStateQuarantineReport:
    """Observable report for a persisted row that cannot be interpreted."""

    spawn_id: str
    state_path: Path
    validation_errors: tuple[object, ...]


class SpawnStateQuarantined(ValueError):
    """Raised consistently by single-row and collection reads for invalid state."""

    def __init__(self, report: SpawnStateQuarantineReport) -> None:
        self.report = report
        super().__init__(f"Spawn state quarantined: {report.state_path}")


@dataclass(frozen=True)
class Decline:
    """A locked mutator's decision to preserve the current state."""

    reason: str


@dataclass(frozen=True)
class Applied:
    """A locked mutation that persisted one atomic before/after transition."""

    before: SpawnRecord
    after: SpawnRecord


@dataclass(frozen=True)
class Declined:
    """A locked mutation that preserved its authoritative snapshot."""

    snapshot: SpawnRecord
    reason: str


@dataclass(frozen=True)
class Missing:
    """The spawn did not exist when the mutation lock was held."""


type LockedMutationResult = Applied | Declined | Missing


def _spawn_dir(spawns_dir: Path, spawn_id: str) -> Path:
    return spawns_dir / spawn_id


def _state_path(spawns_dir: Path, spawn_id: str) -> Path:
    return _spawn_dir(spawns_dir, spawn_id) / "state.json"


def _prompt_path(spawns_dir: Path, spawn_id: str) -> Path:
    return _spawn_dir(spawns_dir, spawn_id) / "starting-prompt.md"


def spawn_lock_path(spawns_dir: Path, spawn_id: str) -> Path:
    """Return the stable external-writer lock for one published spawn."""
    return spawns_dir.parent / "locks" / "spawns" / f"{spawn_id}.lock"


def record_to_stored_state(
    record: SpawnRecord,
) -> StoredSpawnState:
    """Convert a spawn projection to v3 on-disk state without prompt body."""

    return StoredSpawnState.model_validate(
        {
            **record.model_dump(exclude={"prompt"}),
            "v": 3,
            "prompt_length": len(record.prompt) if record.prompt is not None else None,
        }
    )


def stored_state_to_record(
    stored: StoredSpawnState,
    prompt: str | None = None,
) -> SpawnRecord:
    """Convert v3 on-disk state to a ``SpawnRecord`` projection."""

    return SpawnRecord.model_validate(
        {**stored.model_dump(exclude={"v", "prompt_length"}), "prompt": prompt}
    )


def read_prompt(spawns_dir: Path, spawn_id: str) -> str | None:
    """Read a spawn's starting prompt body, if present."""

    path = _prompt_path(spawns_dir, spawn_id)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _read_stored_state(spawns_dir: Path, spawn_id: str) -> StoredSpawnState | None:
    path = _state_path(spawns_dir, spawn_id)
    try:
        try:
            parsed: object = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise LegacySpawnStateUpgradeError(
                    "invalid_root", "spawn state root must be an object"
                )
            raw = cast("dict[str, Any]", parsed)
            candidate = upgrade_legacy_spawn_state(raw) if raw.get("v", 2) == 2 else raw
            return StoredSpawnState.model_validate(candidate)
        except (json.JSONDecodeError, LegacySpawnStateUpgradeError, ValidationError) as exc:
            if isinstance(exc, ValidationError):
                errors: tuple[object, ...] = tuple(exc.errors(include_url=False))
            elif isinstance(exc, LegacySpawnStateUpgradeError):
                errors = (
                    {
                        "type": "legacy_upgrade_error",
                        "loc": exc.fields,
                        "msg": str(exc),
                        "rule": exc.rule,
                    },
                )
            else:
                errors = (
                    {
                        "type": "json_invalid",
                        "loc": (exc.lineno, exc.colno),
                        "msg": str(exc),
                    },
                )
            report = SpawnStateQuarantineReport(
                spawn_id=spawn_id,
                state_path=path,
                validation_errors=errors,
            )
            raise SpawnStateQuarantined(report) from exc
    except FileNotFoundError:
        return None


def read_state(
    spawns_dir: Path,
    spawn_id: str,
    *,
    include_prompt: bool = True,
) -> SpawnRecord | None:
    """Read ``spawns/<id>/state.json`` and reconstruct a spawn record.

    List and filter paths should pass ``include_prompt=False`` so large
    ``starting-prompt.md`` bodies are not read unless a caller needs them.
    """

    stored = _read_stored_state(spawns_dir, spawn_id)
    if stored is None:
        return None
    prompt = read_prompt(spawns_dir, spawn_id) if include_prompt else None
    return stored_state_to_record(stored, prompt=prompt)


def _write_state(spawns_dir: Path, record: SpawnRecord) -> None:
    """Persist a record whose current-state transition was decided by the caller."""

    stored = record_to_stored_state(record)
    atomic_write_text(
        _state_path(spawns_dir, record.id),
        stored.model_dump_json(indent=2) + "\n",
    )


def write_state_locked(
    spawns_dir: Path,
    spawn_id: str,
    mutator: Callable[[SpawnRecord], SpawnRecord | Decline],
    *,
    allow_terminal_overwrite: bool = False,
) -> LockedMutationResult:
    """Re-read, mutate, and persist one spawn under its stable per-spawn lock.

    Lock reentrancy is forbidden because a nested mutation could commit from a
    second snapshot and then be clobbered by the outer mutation's stale result.
    """

    with lock_file(spawn_lock_path(spawns_dir, spawn_id), reentrant=False):
        current = read_state(spawns_dir, spawn_id)
        if current is None:
            return Missing()
        updated = mutator(current)
        if isinstance(updated, Decline):
            return Declined(snapshot=current, reason=updated.reason)
        if updated.id != spawn_id:
            raise ValueError("Locked state mutator must not change spawn id")
        if current.status in TERMINAL_SPAWN_STATUSES and not allow_terminal_overwrite:
            raise ValueError(f"Refusing to overwrite terminal spawn state: {spawn_id}")
        _write_state(spawns_dir, updated)
        return Applied(before=current, after=updated)


def is_safe_spawn_dir_name(name: str) -> bool:
    separators = {"/", "\\", os.sep}
    if os.altsep is not None:
        separators.add(os.altsep)
    return bool(name) and not name.startswith(".") and not any(
        separator in name for separator in separators
    )

def scan_spawn_ids(spawns_dir: Path) -> list[str]:
    """Return child directory names that contain a ``state.json`` file."""

    try:
        entries = os.scandir(spawns_dir)
    except FileNotFoundError:
        return []

    with entries:
        return sorted(
            entry.name
            for entry in entries
            if entry.is_dir() and _state_path(spawns_dir, entry.name).is_file()
        )


__all__ = [
    "Applied",
    "Decline",
    "Declined",
    "LockedMutationResult",
    "Missing",
    "SpawnStateQuarantineReport",
    "SpawnStateQuarantined",
    "StoredSpawnState",
    "is_safe_spawn_dir_name",
    "read_prompt",
    "read_state",
    "record_to_stored_state",
    "scan_spawn_ids",
    "spawn_lock_path",
    "stored_state_to_record",
    "write_state_locked",
]
