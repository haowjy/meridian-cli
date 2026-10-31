"""Autosync artifact storage and transaction boundary.

Single owner of the .meridian/autosync/ file layout. All reads and writes
go through this module. No other module should construct paths into
.meridian/autosync/ or parse its JSON directly.

Mutations are only available through :func:`transaction`, which serializes the
complete autosync workflow for a sync root.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    RootModel,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from meridian.lib.platform.locking import lock_file
from meridian.lib.state.json_doc import mutate_model, read_model, write_model
from meridian.lib.utils.json_shapes import is_object_sequence, json_object
from meridian.plugin_api.fs import AtomicReplaceDurabilityError
from meridian.plugin_api.state import get_user_home

AUTOSYNC_IGNORE_PATTERNS: tuple[str, ...] = (".git", "**/.git")

_DEFAULT_LOCK_TIMEOUT_SECONDS = 60.0


class ConflictRecord(BaseModel):
    """Public view of one stored conflict."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    id: str = ""
    context: str = "unknown"
    sync_root: str = ""
    conflict_type: str = "unknown"
    paths: tuple[str, ...] = ()
    local_sha: str = "unknown"
    remote_sha: str = "unknown"
    remote_branch: str = "main"
    event_name: str = "unknown"
    spawn_id: str | None = None
    created_at: str = "unknown"
    resolved: bool = False
    resolved_at: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten_trigger(cls, value: object) -> object:
        raw = json_object(value)
        if raw is None or "trigger" not in raw:
            return value
        trigger = json_object(raw.get("trigger")) or {}
        return {
            **{key: item for key, item in raw.items() if key != "trigger"},
            "event_name": trigger.get("event", "unknown"),
            "spawn_id": trigger.get("spawn_id"),
        }

    @field_validator("id", mode="before")
    @classmethod
    def _valid_id_or_empty(cls, value: object) -> object:
        return value if isinstance(value, str) and value else ""

    @field_validator(
        "context",
        "sync_root",
        "conflict_type",
        "local_sha",
        "remote_sha",
        "remote_branch",
        "event_name",
        "created_at",
        mode="before",
    )
    @classmethod
    def _stringify(cls, value: object) -> object:
        return str(value)

    @field_validator("paths", mode="before")
    @classmethod
    def _stringify_paths(cls, value: object) -> object:
        return tuple(str(item) for item in value) if is_object_sequence(value) else ()

    @field_validator("spawn_id", "resolved_at", mode="before")
    @classmethod
    def _optional_string(cls, value: object) -> object:
        return None if value is None else str(value)

    @field_validator("resolved", mode="before")
    @classmethod
    def _truthy_bool(cls, value: object) -> object:
        return bool(value)

    @model_serializer(mode="wrap", when_used="json")
    def _serialize_document(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        document = json_object(handler(self)) or {}
        document.pop("event_name", None)
        document.pop("spawn_id", None)
        document["trigger"] = {"event": self.event_name, "spawn_id": self.spawn_id}
        if self.resolved_at is not None:
            document["resolved_at"] = self.resolved_at
        else:
            document.pop("resolved_at", None)
        return document


class _ConflictDocument(RootModel[dict[str, object]]):
    """Lossless conflict document used when mutating an existing file."""


class SyncState(BaseModel):
    """Last sync outcome for a sync root."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    last_sync: str = ""
    outcome: str = "unknown"
    conflict_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_document(cls, value: object) -> object:
        raw = json_object(value)
        if raw is None:
            return value
        last_sync = raw.get("last_sync")
        conflict_id = raw.get("conflict_id")
        return {
            "last_sync": last_sync if isinstance(last_sync, str) and last_sync.strip() else "",
            "outcome": str(raw.get("outcome", "unknown")),
            "conflict_id": None if conflict_id is None else str(conflict_id),
        }


@dataclass(frozen=True)
class SyncRootStatus:
    """Aggregated status for one sync root."""

    state: SyncState | None
    unresolved_conflicts: tuple[ConflictRecord, ...]


def has_autosync_state(sync_root: Path, *, runtime_root: Path) -> bool:
    """Return whether autosync artifacts exist for the sync root."""

    return (
        conflict_dir(sync_root, runtime_root=runtime_root).exists()
        or state_file(sync_root, runtime_root=runtime_root).exists()
    )


def _autosync_root(sync_root: Path, runtime_root: Path) -> Path:
    canonical = sync_root.expanduser().resolve()
    root_hash = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()[:16]
    return runtime_root / "autosync" / root_hash


def conflict_dir(sync_root: Path, *, runtime_root: Path) -> Path:
    """Return the conflict metadata directory for a sync root."""

    return _autosync_root(sync_root, runtime_root) / "conflicts"


def state_file(sync_root: Path, *, runtime_root: Path) -> Path:
    """Return the sync state file path for a sync root."""

    return _autosync_root(sync_root, runtime_root) / "state.json"


def autosync_lock_path(sync_root: Path) -> Path:
    """Return the canonical transaction lock path for a sync root."""

    canonical_root = sync_root.expanduser().resolve()
    root_hash = hashlib.sha256(str(canonical_root).encode("utf-8")).hexdigest()[:16]
    return get_user_home() / "locks" / f"clone-{root_hash}.lock"


class AutosyncMutation(Protocol):
    """Autosync mutations available only inside :func:`transaction`."""

    @property
    def runtime_root(self) -> Path: ...

    def write_conflict(self, record: ConflictRecord) -> None: ...

    def write_sync_state(
        self,
        *,
        outcome: str,
        conflict_id: str | None = None,
    ) -> None: ...

    def mark_resolved(self, conflict_id: str) -> bool: ...


@dataclass(frozen=True)
class _LockedAutosyncTransaction:
    """Concrete mutation capability constructed only while its lock is held."""

    sync_root: Path
    runtime_root: Path

    def write_conflict(self, record: ConflictRecord) -> None:
        """Write one conflict metadata JSON atomically."""

        _write_conflict(self.sync_root, record, runtime_root=self.runtime_root)

    def write_sync_state(
        self,
        *,
        outcome: str,
        conflict_id: str | None = None,
    ) -> None:
        """Write autosync state JSON atomically."""

        _write_sync_state(
            self.sync_root,
            outcome=outcome,
            conflict_id=conflict_id,
            runtime_root=self.runtime_root,
        )

    def mark_resolved(self, conflict_id: str) -> bool:
        """Mark one conflict resolved within this transaction."""

        return _mark_resolved(self.sync_root, conflict_id, runtime_root=self.runtime_root)


@contextmanager
def transaction(
    sync_root: Path,
    *,
    runtime_root: Path,
    timeout: float | None = _DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Generator[AutosyncMutation, None, None]:
    """Hold the canonical sync-root lock and yield its mutation capability."""

    canonical_root = sync_root.expanduser().resolve()
    with lock_file(
        autosync_lock_path(canonical_root),
        timeout=timeout,
        reentrant=True,
    ):
        yield _LockedAutosyncTransaction(canonical_root, runtime_root)


def read_sync_state(sync_root: Path, *, runtime_root: Path) -> SyncState | None:
    """Read sync state for one root."""
    return read_model(state_file(sync_root, runtime_root=runtime_root), SyncState)


def read_conflicts(sync_root: Path, *, runtime_root: Path) -> list[ConflictRecord]:
    """Read all conflict records for one sync root."""

    root = conflict_dir(sync_root, runtime_root=runtime_root)
    if not root.exists():
        return []

    try:
        files = sorted(root.iterdir())
    except OSError:
        return []

    records: list[ConflictRecord] = []
    for file_path in files:
        if file_path.suffix != ".json":
            continue
        record = _read_conflict_record(file_path)
        if record is None:
            continue
        records.append(
            record.model_copy(
                update={
                    "id": record.id or file_path.stem,
                    "sync_root": record.sync_root or sync_root.as_posix(),
                }
            )
        )

    return records


def read_unresolved_conflicts(sync_root: Path, *, runtime_root: Path) -> list[ConflictRecord]:
    """Read unresolved conflict records for one sync root."""
    records = read_conflicts(sync_root, runtime_root=runtime_root)
    return [record for record in records if not record.resolved]


def find_conflict_by_id(
    sync_roots: list[Path],
    conflict_id: str,
    *,
    runtime_root: Path,
) -> tuple[Path | None, ConflictRecord | None]:
    """Find one conflict ID across provided sync roots."""

    for sync_root in sync_roots:
        for record in read_conflicts(sync_root, runtime_root=runtime_root):
            if record.id == conflict_id:
                return sync_root, record
    return None, None


def read_status(sync_root: Path, *, runtime_root: Path) -> SyncRootStatus:
    """Read aggregate autosync status for one sync root."""

    unresolved = tuple(read_unresolved_conflicts(sync_root, runtime_root=runtime_root))
    return SyncRootStatus(
        state=read_sync_state(sync_root, runtime_root=runtime_root),
        unresolved_conflicts=unresolved,
    )


def has_unresolved_conflict(sync_root: Path, *, runtime_root: Path) -> bool:
    """Return whether any unresolved conflict exists for this root."""
    records = read_conflicts(sync_root, runtime_root=runtime_root)
    return any(not record.resolved for record in records)


def generate_conflict_id() -> str:
    """Generate a conflict ID like c20260512-001."""

    date_str = datetime.now(UTC).strftime("%Y%m%d")
    short_hash = hashlib.sha256(str(time.monotonic_ns()).encode("utf-8")).hexdigest()[:3]
    return f"c{date_str}-{short_hash}"


def _write_conflict(sync_root: Path, record: ConflictRecord, *, runtime_root: Path) -> None:
    """Write one conflict metadata JSON atomically."""
    target = conflict_dir(sync_root, runtime_root=runtime_root) / f"{record.id}.json"
    write_model(target, record, indent=2)


def _write_sync_state(
    sync_root: Path,
    *,
    outcome: str,
    conflict_id: str | None = None,
    runtime_root: Path,
) -> None:
    """Write autosync state JSON atomically."""

    write_model(
        state_file(sync_root, runtime_root=runtime_root),
        SyncState(
            last_sync=datetime.now(UTC).isoformat(),
            outcome=outcome,
            conflict_id=conflict_id,
        ),
        indent=2,
    )


def _mark_resolved(sync_root: Path, conflict_id: str, *, runtime_root: Path) -> bool:
    """Mark one conflict as resolved. Returns True when metadata was updated."""

    try:
        target = _find_conflict_file(sync_root, conflict_id, runtime_root=runtime_root)
    except OSError:
        return False
    if target is None:
        return False

    expected: _ConflictDocument | None = None

    def resolve(
        document: _ConflictDocument | None,
    ) -> tuple[_ConflictDocument | None, bool]:
        nonlocal expected
        if document is None:
            return None, False
        expected = _ConflictDocument.model_validate(
            {
                **document.root,
                "resolved": True,
                "resolved_at": datetime.now(UTC).isoformat(),
            }
        )
        return expected, True

    try:
        _updated, resolved = mutate_model(
            target,
            _ConflictDocument,
            resolve,
            lock=lock_file(autosync_lock_path(sync_root), reentrant=True),
            indent=2,
        )
        return resolved
    except AtomicReplaceDurabilityError:
        return expected is not None and read_model(target, _ConflictDocument) == expected
    except OSError:
        return False


def _read_conflict_record(path: Path) -> ConflictRecord | None:
    document = read_model(path, _ConflictDocument)
    if document is None:
        return None
    try:
        return ConflictRecord.model_validate(document.root)
    except ValidationError:
        return None


def _find_conflict_file(
    sync_root: Path,
    conflict_id: str,
    *,
    runtime_root: Path,
) -> Path | None:
    root = conflict_dir(sync_root, runtime_root=runtime_root)
    direct = root / f"{conflict_id}.json"
    direct_record = _read_conflict_record(direct)
    if direct_record is not None:
        return direct

    for file_path in root.glob("*.json"):
        record = _read_conflict_record(file_path)
        if record is None:
            continue
        if (record.id or file_path.stem) == conflict_id:
            return file_path
    return None


__all__ = [
    "AUTOSYNC_IGNORE_PATTERNS",
    "AutosyncMutation",
    "ConflictRecord",
    "SyncRootStatus",
    "SyncState",
    "autosync_lock_path",
    "conflict_dir",
    "find_conflict_by_id",
    "generate_conflict_id",
    "has_autosync_state",
    "has_unresolved_conflict",
    "read_conflicts",
    "read_status",
    "read_sync_state",
    "read_unresolved_conflicts",
    "state_file",
    "transaction",
]
