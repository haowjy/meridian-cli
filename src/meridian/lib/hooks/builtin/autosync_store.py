"""Autosync artifact storage and transaction boundary.

Single owner of the .meridian/autosync/ file layout. All reads and writes
go through this module. No other module should construct paths into
.meridian/autosync/ or parse its JSON directly.

Mutations are only available through :func:`transaction`, which serializes the
complete autosync workflow for a sync root.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from meridian.lib.platform.locking import lock_file
from meridian.plugin_api.fs import AtomicReplaceDurabilityError, atomic_write_text
from meridian.plugin_api.state import get_user_home

AUTOSYNC_IGNORE_PATTERNS: tuple[str, ...] = (".git", "**/.git", ".meridian/autosync/")

_DEFAULT_LOCK_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class ConflictRecord:
    """One conflict, as stored on disk."""

    id: str
    context: str
    sync_root: str
    conflict_type: str
    paths: tuple[str, ...]
    local_sha: str
    remote_sha: str
    remote_branch: str
    event_name: str
    spawn_id: str | None
    created_at: str
    resolved: bool
    resolved_at: str | None = None


@dataclass(frozen=True)
class SyncState:
    """Last sync outcome for a sync root."""

    last_sync: str
    outcome: str
    conflict_id: str | None = None


@dataclass(frozen=True)
class SyncRootStatus:
    """Aggregated status for one sync root."""

    state: SyncState | None
    unresolved_conflicts: tuple[ConflictRecord, ...]


def has_autosync_state(sync_root: Path) -> bool:
    """Return whether autosync artifacts exist for the sync root."""

    return conflict_dir(sync_root).exists() or state_file(sync_root).exists()


def conflict_dir(sync_root: Path) -> Path:
    """Return the conflict metadata directory for a sync root."""

    return sync_root / ".meridian" / "autosync" / "conflicts"


def state_file(sync_root: Path) -> Path:
    """Return the sync state file path for a sync root."""

    return sync_root / ".meridian" / "autosync" / "state.json"


def autosync_lock_path(sync_root: Path) -> Path:
    """Return the canonical transaction lock path for a sync root."""

    canonical_root = sync_root.expanduser().resolve()
    root_hash = hashlib.sha256(str(canonical_root).encode("utf-8")).hexdigest()[:16]
    return get_user_home() / "locks" / f"clone-{root_hash}.lock"


class AutosyncMutation(Protocol):
    """Autosync mutations available only inside :func:`transaction`."""

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

    def write_conflict(self, record: ConflictRecord) -> None:
        """Write one conflict metadata JSON atomically."""

        _write_conflict(self.sync_root, record)

    def write_sync_state(
        self,
        *,
        outcome: str,
        conflict_id: str | None = None,
    ) -> None:
        """Write autosync state JSON atomically."""

        _write_sync_state(self.sync_root, outcome=outcome, conflict_id=conflict_id)

    def mark_resolved(self, conflict_id: str) -> bool:
        """Mark one conflict resolved within this transaction."""

        return _mark_resolved(self.sync_root, conflict_id)


@contextmanager
def transaction(
    sync_root: Path,
    *,
    timeout: float | None = _DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Generator[AutosyncMutation, None, None]:
    """Hold the canonical sync-root lock and yield its mutation capability."""

    canonical_root = sync_root.expanduser().resolve()
    with lock_file(
        autosync_lock_path(canonical_root),
        timeout=timeout,
        reentrant=True,
    ):
        yield _LockedAutosyncTransaction(canonical_root)


def read_sync_state(sync_root: Path) -> SyncState | None:
    """Read sync state for one root."""

    target = state_file(sync_root)
    if not target.exists():
        return None
    data = _load_json_object(target)
    if data is None:
        return None

    last_sync_raw = data.get("last_sync")
    last_sync = last_sync_raw if isinstance(last_sync_raw, str) and last_sync_raw.strip() else ""

    return SyncState(
        last_sync=last_sync,
        outcome=str(data.get("outcome", "unknown")),
        conflict_id=_to_optional_str(data.get("conflict_id")),
    )


def read_conflicts(sync_root: Path) -> list[ConflictRecord]:
    """Read all conflict records for one sync root."""

    root = conflict_dir(sync_root)
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
        data = _load_json_object(file_path)
        if data is None:
            continue

        conflict_id_raw = data.get("id")
        conflict_id = (
            conflict_id_raw
            if isinstance(conflict_id_raw, str) and conflict_id_raw
            else file_path.stem
        )

        trigger_obj = _as_object_dict(data.get("trigger")) or {}
        event_name = str(trigger_obj.get("event", "unknown"))
        spawn_id = _to_optional_str(trigger_obj.get("spawn_id"))

        paths = _to_string_tuple(data.get("paths"))

        records.append(
            ConflictRecord(
                id=conflict_id,
                context=str(data.get("context", "unknown")),
                sync_root=str(data.get("sync_root", sync_root.as_posix())),
                conflict_type=str(data.get("conflict_type", "unknown")),
                paths=paths,
                local_sha=str(data.get("local_sha", "unknown")),
                remote_sha=str(data.get("remote_sha", "unknown")),
                remote_branch=str(data.get("remote_branch", "main")),
                event_name=event_name,
                spawn_id=spawn_id,
                created_at=str(data.get("created_at", "unknown")),
                resolved=bool(data.get("resolved", False)),
                resolved_at=_to_optional_str(data.get("resolved_at")),
            )
        )

    return records


def read_unresolved_conflicts(sync_root: Path) -> list[ConflictRecord]:
    """Read unresolved conflict records for one sync root."""

    return [record for record in read_conflicts(sync_root) if not record.resolved]


def find_conflict_by_id(
    sync_roots: list[Path],
    conflict_id: str,
) -> tuple[Path | None, ConflictRecord | None]:
    """Find one conflict ID across provided sync roots."""

    for sync_root in sync_roots:
        for record in read_conflicts(sync_root):
            if record.id == conflict_id:
                return sync_root, record
    return None, None


def read_status(sync_root: Path) -> SyncRootStatus:
    """Read aggregate autosync status for one sync root."""

    unresolved = tuple(read_unresolved_conflicts(sync_root))
    return SyncRootStatus(state=read_sync_state(sync_root), unresolved_conflicts=unresolved)


def has_unresolved_conflict(sync_root: Path) -> bool:
    """Return whether any unresolved conflict exists for this root."""

    return any(not record.resolved for record in read_conflicts(sync_root))


def generate_conflict_id() -> str:
    """Generate a conflict ID like c20260512-001."""

    date_str = datetime.now(UTC).strftime("%Y%m%d")
    short_hash = hashlib.sha256(str(time.monotonic_ns()).encode("utf-8")).hexdigest()[:3]
    return f"c{date_str}-{short_hash}"


def _write_conflict(sync_root: Path, record: ConflictRecord) -> None:
    """Write one conflict metadata JSON atomically."""

    target_dir = conflict_dir(sync_root)
    target_dir.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "id": record.id,
        "context": record.context,
        "sync_root": record.sync_root,
        "conflict_type": record.conflict_type,
        "paths": list(record.paths),
        "local_sha": record.local_sha,
        "remote_sha": record.remote_sha,
        "remote_branch": record.remote_branch,
        "trigger": {
            "event": record.event_name,
            "spawn_id": record.spawn_id,
        },
        "created_at": record.created_at,
        "resolved": record.resolved,
    }
    if record.resolved_at is not None:
        payload["resolved_at"] = record.resolved_at

    target = target_dir / f"{record.id}.json"
    atomic_write_text(target, json.dumps(payload, indent=2))


def _write_sync_state(
    sync_root: Path,
    *,
    outcome: str,
    conflict_id: str | None = None,
) -> None:
    """Write autosync state JSON atomically."""

    target_dir = sync_root / ".meridian" / "autosync"
    target_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "last_sync": datetime.now(UTC).isoformat(),
        "outcome": outcome,
        "conflict_id": conflict_id,
    }
    atomic_write_text(state_file(sync_root), json.dumps(payload, indent=2))


def _mark_resolved(sync_root: Path, conflict_id: str) -> bool:
    """Mark one conflict as resolved. Returns True when metadata was updated."""

    try:
        target, data = _find_conflict_file(sync_root, conflict_id)
    except OSError:
        return False
    if target is None or data is None:
        return False

    data["resolved"] = True
    data["resolved_at"] = datetime.now(UTC).isoformat()
    content = json.dumps(data, indent=2)
    try:
        return _write_text_reconciled(target, content)
    except OSError:
        return False


def _write_text_reconciled(path: Path, content: str) -> bool:
    """Write content, verifying visibility after a post-commit durability error."""

    try:
        atomic_write_text(path, content)
    except AtomicReplaceDurabilityError:
        try:
            return path.read_text(encoding="utf-8") == content
        except OSError:
            return False
    return True


def _load_json_object(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    typed_raw = cast("dict[object, object]", raw)
    return {str(key): value for key, value in typed_raw.items()}


def _as_object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    typed = cast("dict[object, object]", value)
    return {str(key): item for key, item in typed.items()}


def _to_string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    typed = cast("list[object]", value)
    return tuple(str(item) for item in typed)


def _to_optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _find_conflict_file(
    sync_root: Path,
    conflict_id: str,
) -> tuple[Path | None, dict[str, Any] | None]:
    root = conflict_dir(sync_root)
    direct = root / f"{conflict_id}.json"
    direct_data = _load_json_object(direct) if direct.exists() else None
    if direct_data is not None:
        return direct, direct_data

    for file_path in root.glob("*.json"):
        data = _load_json_object(file_path)
        if data is None:
            continue
        if str(data.get("id", file_path.stem)) == conflict_id:
            return file_path, data
    return None, None


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
