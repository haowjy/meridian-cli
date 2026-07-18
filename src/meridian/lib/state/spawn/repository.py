"""Spawn v2 state-file persistence helpers.

The helpers in this module persist one ``state.json`` per spawn under the
runtime spawns directory.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError, model_validator

from meridian.lib.core.domain import ALL_SPAWN_STATUSES, TERMINAL_SPAWN_STATUSES
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.spawn.model import _LAUNCH_MODE_VALUES, SpawnRecord, SpawnStateFields
from meridian.lib.state.spawn.model import AUTHORITATIVE_ORIGINS as _AUTHORITATIVE_ORIGINS

_PERSISTED_STATUS_VALUES = ALL_SPAWN_STATUSES | {"unknown"}


class StoredSpawnState(SpawnStateFields):
    """On-disk v2 ``state.json`` representation.

    The prompt body is stored separately in ``starting-prompt.md``; this model
    keeps only ``prompt_length`` metadata so state reads can stay lightweight.
    """

    v: Literal[2]
    prompt_length: int | None = None

    @model_validator(mode="before")
    @classmethod
    def quarantine_unknown_vocabulary(cls, value: Any) -> Any:
        """Reject, rather than reinterpret, rows with unknown vocabulary values."""

        if not isinstance(value, dict):
            return value
        vocabularies = {
            "status": _PERSISTED_STATUS_VALUES,
            "kind": {"child", "primary", "streaming"},
            "launch_mode": _LAUNCH_MODE_VALUES,
        }
        invalid = {
            field: raw
            for field, allowed in vocabularies.items()
            if (raw := value.get(field)) is not None
            and (not isinstance(raw, str) or raw not in allowed)
        }
        if invalid:
            raise ValueError(f"quarantined unknown spawn vocabulary: {invalid}")
        nested_vocabularies: tuple[tuple[str, str, frozenset[object]], ...] = (
            ("runner_exit", "status", TERMINAL_SPAWN_STATUSES),
            ("terminal", "status", TERMINAL_SPAWN_STATUSES),
            ("terminal", "origin", _AUTHORITATIVE_ORIGINS | {"reconciler"}),
        )
        nested_invalid: dict[str, object] = {}
        for container_name, field, allowed in nested_vocabularies:
            container = value.get(container_name)
            if not isinstance(container, dict):
                continue
            raw = container.get(field)
            if raw is not None and (not isinstance(raw, str) or raw not in allowed):
                nested_invalid[f"{container_name}.{field}"] = raw
        if nested_invalid:
            raise ValueError(f"quarantined unknown spawn vocabulary: {nested_invalid}")
        return value


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


def _enforce_spawn_state_field_accounting(
    *,
    shared_fields: set[str] | None = None,
    stored_fields: set[str] | None = None,
    record_fields: set[str] | None = None,
) -> None:
    """Fail at import when either projection stops accounting for a shared field."""

    expected = shared_fields if shared_fields is not None else set(SpawnStateFields.model_fields)
    stored = stored_fields if stored_fields is not None else set(StoredSpawnState.model_fields)
    record = record_fields if record_fields is not None else set(SpawnRecord.model_fields)
    stored_shared = stored - {"v", "prompt_length"}
    record_shared = record - {"prompt"}
    missing_stored = expected - stored_shared
    stale_stored = stored_shared - expected
    missing_record = expected - record_shared
    stale_record = record_shared - expected
    if missing_stored or stale_stored or missing_record or stale_record:
        raise ImportError(
            "Spawn state field-accounting drift. "
            f"Stored missing={sorted(missing_stored)}, stale={sorted(stale_stored)}. "
            f"Record missing={sorted(missing_record)}, stale={sorted(stale_record)}."
        )


_enforce_spawn_state_field_accounting()



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
    """Convert a spawn projection to v2 on-disk state without prompt body."""

    return StoredSpawnState.model_validate(
        {
            **record.model_dump(exclude={"prompt"}),
            "v": 2,
            "prompt_length": len(record.prompt) if record.prompt is not None else None,
        }
    )


def stored_state_to_record(
    stored: StoredSpawnState,
    prompt: str | None = None,
) -> SpawnRecord:
    """Convert v2 on-disk state to a ``SpawnRecord`` projection."""

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
            return StoredSpawnState.model_validate_json(path.read_text(encoding="utf-8"))
        except ValidationError as exc:
            report = SpawnStateQuarantineReport(
                spawn_id=spawn_id,
                state_path=path,
                validation_errors=tuple(exc.errors(include_url=False)),
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
    mutator: Callable[[SpawnRecord], SpawnRecord],
    *,
    allow_terminal_overwrite: bool = False,
) -> SpawnRecord:
    """Re-read, mutate, and persist one spawn under its stable per-spawn lock.

    Lock reentrancy is forbidden because a nested mutation could commit from a
    second snapshot and then be clobbered by the outer mutation's stale result.
    """

    with lock_file(spawn_lock_path(spawns_dir, spawn_id), reentrant=False):
        current = read_state(spawns_dir, spawn_id)
        if current is None:
            raise FileNotFoundError(_state_path(spawns_dir, spawn_id))
        updated = mutator(current)
        if updated.id != spawn_id:
            raise ValueError("Locked state mutator must not change spawn id")
        if current.status in TERMINAL_SPAWN_STATUSES and not allow_terminal_overwrite:
            raise ValueError(f"Refusing to overwrite terminal spawn state: {spawn_id}")
        _write_state(spawns_dir, updated)
        return updated


def is_safe_spawn_dir_name(name: str) -> bool:
    separators = {"/", "\\", os.sep}
    if os.altsep is not None:
        separators.add(os.altsep)
    return bool(name) and not name.startswith(".") and not any(
        separator in name for separator in separators
    )

def scan_spawn_ids(spawns_dir: Path) -> list[str]:
    """Return child directory names that contain a v2 ``state.json`` file."""

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
