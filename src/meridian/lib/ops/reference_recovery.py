"""Read-only harness session ID recovery for tracked references.

When a tracked spawn or chat reference lacks a recorded harness session ID,
this module reads only exact recorded state. Native filesystem discovery and
primary metadata mirrors cannot create or replace chat bindings.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from meridian.lib.state import session_identity, session_store, spawn_store


class RecoveryProvenance(StrEnum):
    """Source of a recovered harness session ID."""

    SESSION_STORE = "session_store"
    SPAWN_ROW = "spawn_row"


@dataclass(frozen=True)
class RecoveryResult:
    """Result of attempting to recover a harness session ID."""

    harness_session_id: str
    provenance: RecoveryProvenance
    supporting_chat_id: str | None = None


def _normalize(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def recover_recorded_chat_harness_session_id(
    runtime_root: Path, chat_id: str, *, session: session_store.SessionRecord | None = None,
) -> RecoveryResult | None:
    """Read only the immutable chat binding; mirrors cannot supply missing identity."""
    record = session or session_store.get_session_record(runtime_root, chat_id)
    return _recover_from_session_record(record) if record is not None else None


def recover_recorded_chat_harness_session_ids(
    runtime_root: Path, sessions: Sequence[session_store.SessionRecord],
) -> dict[str, RecoveryResult]:
    return {session.chat_id: result for session in sessions
            if (result := _recover_from_session_record(session)) is not None}


def _recover_from_session_record(
    session: session_store.SessionRecord,
) -> RecoveryResult | None:
    if session.record_mode == "historical":
        return None
    session_id = _normalize(session.harness_session_id)
    if session_id is None:
        return None
    return RecoveryResult(
        harness_session_id=session_id,
        provenance=RecoveryProvenance.SESSION_STORE,
        supporting_chat_id=session.chat_id,
    )


def _recover_from_spawn_row(runtime_root: Path, spawn_id: str) -> RecoveryResult | None:
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    if row is None:
        return None
    session_id = _normalize(row.harness_session_id)
    if session_id:
        return RecoveryResult(
            harness_session_id=session_id,
            provenance=RecoveryProvenance.SPAWN_ROW,
        )
    return None


def recover_harness_session_id(
    *,
    project_root: Path,
    runtime_root: Path,
    ref: str,
    recorded_harness_session_id: str | None = None,
    recorded_harness: str | None = None,
) -> RecoveryResult | None:
    """Read exact recorded identity only. Never infer a chat binding from native files."""
    if recorded_harness_session_id and recorded_harness_session_id.strip():
        return None

    normalized_ref = ref.strip()
    if not normalized_ref:
        return None

    # Chat reference
    if normalized_ref.startswith("c") and normalized_ref[1:].isdigit():
        return recover_recorded_chat_harness_session_id(runtime_root, normalized_ref)

    # Spawn reference
    if normalized_ref.startswith("p") and normalized_ref[1:].isdigit():
        result = _recover_from_spawn_row(runtime_root, normalized_ref)
        if result is not None:
            return result

        row = spawn_store.get_spawn(runtime_root, normalized_ref)
        if row is not None:
            linked_record = session_identity.get_session_record_for_spawn(
                runtime_root,
                normalized_ref,
                require_harness_session_id=True,
            )
            if linked_record is not None:
                session_id = _normalize(linked_record.harness_session_id)
                if session_id:
                    return RecoveryResult(
                        harness_session_id=session_id,
                        provenance=RecoveryProvenance.SESSION_STORE,
                        supporting_chat_id=linked_record.chat_id,
                    )

        return None

    return None


__all__ = [
    "RecoveryProvenance",
    "RecoveryResult",
    "recover_harness_session_id",
    "recover_recorded_chat_harness_session_id",
    "recover_recorded_chat_harness_session_ids",
]
