"""Canonical accessors for session vs owner-chat identity.

Two concepts:
- **Exact session** — the chat id for this specific session (`chat_id` on rows).
- **Owner session** — the primary/session-family chat id (`owner_chat_id` on spawns).
"""

from __future__ import annotations

from pathlib import Path

from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.session_store import SessionRecord
from meridian.lib.state.spawn.model import SpawnRecord


def _normalized(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def spawn_exact_chat_id(row: SpawnRecord) -> str | None:
    """Return the exact session chat id recorded on a spawn row."""

    return _normalized(row.chat_id)


def spawn_owner_chat_id(row: SpawnRecord) -> str | None:
    """Return the owner/session-family chat id for a spawn row."""

    owner = _normalized(row.owner_chat_id)
    if owner is not None:
        return owner
    return spawn_exact_chat_id(row)


def session_exact_chat_id(record: SessionRecord) -> str:
    """Return the exact session chat id for a session record."""

    return record.chat_id.strip()


def session_owner_chat_id(
    runtime_root: Path,
    record: SessionRecord,
    *,
    spawn_row: SpawnRecord | None = None,
) -> str | None:
    """Return the owner/session-family chat id for a session record."""

    if record.kind == "primary":
        return session_exact_chat_id(record) or None

    linked_spawn = spawn_row
    if linked_spawn is None and record.spawn_id:
        linked_spawn = spawn_store.get_spawn(runtime_root, record.spawn_id)
    if linked_spawn is not None:
        return spawn_owner_chat_id(linked_spawn)

    forked = _normalized(record.forked_from_chat_id)
    if forked is not None:
        return forked

    return session_exact_chat_id(record) or None


def get_owner_chat_for_session(
    runtime_root: Path,
    chat_id: str,
) -> str | None:
    """Resolve owner/session-family chat id for an exact session chat id."""

    normalized = _normalized(chat_id)
    if normalized is None:
        return None
    record = session_store.get_session_record(runtime_root, normalized)
    if record is None:
        return None
    return session_owner_chat_id(runtime_root, record)


def is_tracked_chat_ref(runtime_root: Path, ref: str) -> bool:
    """Return whether ``ref`` is a Meridian chat/session id in this runtime root."""

    normalized = _normalized(ref)
    if normalized is None or not normalized.startswith("c"):
        return False
    if normalized[1:].isdigit():
        return True
    return session_store.get_session_record(runtime_root, normalized) is not None


def get_session_record_for_spawn(
    runtime_root: Path,
    spawn_id: str,
    *,
    require_harness_session_id: bool = False,
) -> SessionRecord | None:
    """Return the session record linked to a spawn via ``spawn_id``."""

    normalized_spawn_id = _normalized(spawn_id)
    if normalized_spawn_id is None:
        return None

    for record in session_store.list_all_session_records(runtime_root):
        if record.spawn_id != normalized_spawn_id:
            continue
        if require_harness_session_id and not record.harness_session_id.strip():
            continue
        return record

    row = spawn_store.get_spawn(runtime_root, normalized_spawn_id)
    if row is None:
        return None
    exact_chat_id = spawn_exact_chat_id(row)
    if exact_chat_id is None:
        return None

    record = session_store.get_session_record(runtime_root, exact_chat_id)
    if record is None:
        return None
    linked_spawn_id = _normalized(record.spawn_id)
    if linked_spawn_id is not None:
        if linked_spawn_id != normalized_spawn_id:
            return None
    elif row.kind == "child":
        # Child spawns may reuse the owner/primary chat id without sharing its transcript.
        return None
    if require_harness_session_id and not record.harness_session_id.strip():
        return None
    return record


def spawn_matches_owner_chat(row: SpawnRecord, owner_chat_id: str) -> bool:
    """Return whether a spawn belongs to the given owner/session-family chat id."""

    return (spawn_owner_chat_id(row) or "") == (owner_chat_id or "").strip()


def spawn_matches_exact_session(row: SpawnRecord, session_chat_id: str) -> bool:
    """Return whether a spawn belongs to the given exact session chat id."""

    return (spawn_exact_chat_id(row) or "") == (session_chat_id or "").strip()


def list_spawns_for_owner_chat(runtime_root: Path, owner_chat_id: str) -> list[SpawnRecord]:
    """List spawn rows whose owner/session-family chat id matches."""

    return spawn_store.list_spawns(runtime_root, filters={"owner_chat_id": owner_chat_id})


def list_spawns_for_exact_session(runtime_root: Path, session_chat_id: str) -> list[SpawnRecord]:
    """List spawn rows whose exact session chat id matches."""

    return spawn_store.list_spawns(runtime_root, filters={"chat_id": session_chat_id})


__all__ = [
    "get_owner_chat_for_session",
    "get_session_record_for_spawn",
    "is_tracked_chat_ref",
    "list_spawns_for_exact_session",
    "list_spawns_for_owner_chat",
    "session_exact_chat_id",
    "session_owner_chat_id",
    "spawn_exact_chat_id",
    "spawn_matches_exact_session",
    "spawn_matches_owner_chat",
    "spawn_owner_chat_id",
]
