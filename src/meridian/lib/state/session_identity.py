"""Canonical accessors for session vs owner-chat identity.

Two concepts:
- **Exact session** — the chat id for this specific session (`chat_id` on rows).
- **Owner session** — the primary/session-family chat id (`owner_chat_id` on spawns).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from meridian.lib.core.types import SpawnId, normalize_optional_identity
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.primary_meta import read_primary_harness_session_id
from meridian.lib.state.session_store import SessionRecord
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_status import quarantine_may_observe


def spawn_exact_chat_id(row: SpawnRecord) -> str | None:
    """Return the exact session chat id recorded on a spawn row, or None if it has none."""

    return row.entry_chat_id


def spawn_owner_chat_id(row: SpawnRecord) -> str | None:
    """Return the owner/session-family chat id for a spawn row."""

    return row.owner_chat_id or row.chat_id


def session_exact_chat_id(record: SessionRecord) -> str:
    """Return the exact session chat id for a session record."""

    return record.chat_id


def session_owner_chat_id(
    runtime_root: Path,
    record: SessionRecord,
    *,
    spawn_row: SpawnRecord | None = None,
) -> str | None:
    """Return the owner/session-family chat id for a session record."""

    if record.kind == "primary":
        return session_exact_chat_id(record)

    linked_spawn = spawn_row
    if linked_spawn is None and record.spawn_id:
        linked_spawn = spawn_store.get_spawn(runtime_root, record.spawn_id)
    if linked_spawn is not None:
        return spawn_owner_chat_id(linked_spawn)

    if record.forked_from_chat_id is not None:
        return record.forked_from_chat_id

    return session_exact_chat_id(record)


def chat_owner_spawn(
    runtime_root: Path,
    chat_id: str,
    *,
    newest: SessionRecord | None = None,
) -> SpawnId | None:
    """Return the spawn that owns ``chat_id``: the one that ran it, whether or not it still runs.

    Every generation records the spawn that executed it at its start, so the answer is
    the newest generation whose spawn row still exists; a link to a row that was never
    written (a crash between reserving the ID and writing the row) is skipped. Pass the
    chat's already-read ``newest`` record to answer a linked chat without reading the
    rest. Which run is live in the chat right now is a different question, not asked here.
    """
    if newest is not None and newest.spawn_id and spawn_store.get_spawn(
        runtime_root, newest.spawn_id
    ):
        return SpawnId(newest.spawn_id)
    for generation in reversed(session_store.chat_generations(runtime_root, chat_id)):
        if generation.spawn_id and spawn_store.get_spawn(runtime_root, generation.spawn_id):
            return SpawnId(generation.spawn_id)
    return None


def session_records_for_spawns(
    root: Path, records: Iterable[SpawnRecord]
) -> dict[str, SessionRecord]:
    """Return unmodified authoritative generations; ambiguous linkage is a conflict.

    Do not fill nullable identity fields here: capture fingerprints, historical
    provenance and publication witnesses must detect changes to those fields too.
    """
    # A row with no session of its own (chat-less by design, or a legacy placeholder
    # chat) has none to find; only older chat-less rows still need the full scan.
    records = tuple(row for row in records if not row.has_no_session_of_its_own)
    if not records:
        return {}
    chats = {row.chat_id for row in records if row.chat_id}
    generations = session_store.fold_session_generations(
        session_store.chats_rows(
            root, None if any(not row.chat_id for row in records) else chats
        ).ordered,
    )
    linked: dict[str, list[SessionRecord]] = {}
    for session in generations:
        if session.spawn_id:
            linked.setdefault(session.spawn_id, []).append(session)
    exact = {(session.chat_id, session.session_instance_id): session for session in generations}
    result: dict[str, SessionRecord] = {}
    for record in records:
        candidates = linked.get(record.id, [])
        if record.chat_id:
            candidates = [session for session in candidates if session.chat_id == record.chat_id]
        candidates = [
            session for session in candidates if session.history_id in {None, record.history_id}
        ]
        if record.session_instance_id and record.chat_id is not None:
            session = exact.get((record.chat_id, record.session_instance_id))
            if session is None and candidates:
                raise ValueError(f"Session generation conflicts with history: {record.id}")
            candidates = [session] if session is not None else []
        if len(candidates) > 1:
            raise ValueError(f"Ambiguous session metadata for history: {record.id}")
        if candidates:
            session = candidates[0]
            if session.spawn_id not in {None, record.id} or session.history_id not in {
                None,
                record.history_id,
            }:
                raise ValueError(f"Session identity conflicts with history: {record.id}")
            result[record.id] = session
    return result


def native_identity_candidates(
    runtime_root: Path, row: SpawnRecord, session: SessionRecord | None
) -> tuple[set[str], set[str]]:
    """Normalize an aggregate's facts and its already-verified exact generation.

    Selection requires one candidate per dimension. Ownership checks must not
    ignore a possible matching owner merely because its recorded facts conflict.
    Neither caller may discover identity from a newer chat or native file here.
    """
    harnesses = {
        value.strip().lower()
        for value in (row.harness, session.harness if session else None)
        if value and value.strip()
    }
    native_ids = {
        value.strip()
        for value in (
            row.harness_session_id,
            read_primary_harness_session_id(runtime_root, row.id)
            if row.kind == "primary"
            else None,
            session.harness_session_id if session else None,
        )
        if value and value.strip()
    }
    return harnesses, native_ids


def live_observer_for_chat(
    runtime_root: Path,
    chat_id: str,
    *,
    records: Iterable[SpawnRecord] | None = None,
    excluding_spawn_id: str | None = None,
) -> str | None:
    """Find an execution using this conversation without inventing a chat lease."""
    from meridian.lib.state.process_scope_projection import read_scope_projection
    from meridian.lib.state.reaper import scope_liveness

    if records is None:
        scan = spawn_store.list_spawns(runtime_root)
        if any(
            quarantine_may_observe(runtime_root, report, chat_id)
            for report in scan.quarantines
        ):
            raise ValueError("Cannot establish current-chat ownership; run `meridian doctor`")
        records = scan.records
    for row in records:
        if (
            row.record_mode != "live"
            or row.current_chat_id != chat_id
            or row.id == excluding_spawn_id
        ):
            continue
        if row.chat_id and session_store.is_session_lease_owner_alive(
            runtime_root,
            row.chat_id,
            session_instance_id=row.session_instance_id,
        ):
            return row.id
        scopes = read_scope_projection(runtime_root, SpawnId(row.id))
        if any(
            scope_liveness(scope)["likely_serving"]
            for scope in scopes.scopes
            if scope.release_id not in scopes.released_ids
        ):
            return row.id
    return None


def get_session_record_for_spawn(
    runtime_root: Path,
    spawn_id: str,
    *,
    require_harness_session_id: bool = False,
) -> SessionRecord | None:
    """Return the entry generation, not a later conversation observed by the run."""

    normalized_spawn_id = normalize_optional_identity(spawn_id)
    if normalized_spawn_id is None:
        return None

    row = spawn_store.get_spawn(runtime_root, normalized_spawn_id)
    if row is None or row.has_no_session_of_its_own:
        return None
    entry_chat_id = spawn_exact_chat_id(row)
    # An older chat-less row (app spawn, pre-v3) names no session itself: only a
    # generation whose start event names this spawn can be its own.
    generations = (
        session_store.chat_generations(runtime_root, entry_chat_id)
        if entry_chat_id
        else session_store.fold_session_generations(
            session_store.chats_rows(runtime_root, None).ordered,
        )
    )
    if row.session_instance_id:
        return next(
            (
                record
                for record in reversed(generations)
                if record.chat_id == row.chat_id
                and record.session_instance_id == row.session_instance_id
                and record.spawn_id in {None, normalized_spawn_id}
                and (not require_harness_session_id or record.harness_session_id is not None)
            ),
            None,
        )

    for record in reversed(generations):
        if record.spawn_id != normalized_spawn_id:
            continue
        if record.history_id not in {None, row.history_id}:
            continue
        if require_harness_session_id and record.harness_session_id is None:
            continue
        return record

    if entry_chat_id is None:
        return None

    record = session_store.chat_record(runtime_root, entry_chat_id)
    if record is None:
        return None
    linked_spawn_id = normalize_optional_identity(record.spawn_id)
    if linked_spawn_id is not None:
        if linked_spawn_id != normalized_spawn_id:
            return None
    elif row.kind == "child":
        # Child spawns may reuse the owner/primary chat id without sharing its transcript.
        return None
    if require_harness_session_id and record.harness_session_id is None:
        return None
    return record


def spawn_matches_owner_chat(row: SpawnRecord, owner_chat_id: str) -> bool:
    """Return whether a spawn belongs to the given owner/session-family chat id."""

    return spawn_owner_chat_id(row) == normalize_optional_identity(owner_chat_id)


def spawn_matches_exact_session(row: SpawnRecord, session_chat_id: str) -> bool:
    """Return whether a spawn belongs to the given exact session chat id."""

    return spawn_exact_chat_id(row) == normalize_optional_identity(session_chat_id)


__all__ = [
    "chat_owner_spawn",
    "get_session_record_for_spawn",
    "live_observer_for_chat",
    "native_identity_candidates",
    "session_exact_chat_id",
    "session_owner_chat_id",
    "session_records_for_spawns",
    "spawn_exact_chat_id",
    "spawn_matches_exact_session",
    "spawn_matches_owner_chat",
    "spawn_owner_chat_id",
]
