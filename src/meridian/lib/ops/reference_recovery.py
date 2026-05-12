"""Read-only harness session ID recovery for tracked references.

When a tracked spawn or chat reference lacks a recorded harness session ID,
this module provides fallback recovery from durable state and harness
adapters. It does NOT persist recovered IDs; callers decide whether to
write back based on their own verification policies.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.state import primary_meta, session_store, spawn_store
from meridian.lib.state.spawn.model import SpawnRecord


class RecoveryProvenance(StrEnum):
    """Source of a recovered harness session ID."""

    SESSION_STORE = "session_store"
    SPAWN_ROW = "spawn_row"
    PRIMARY_META = "primary_meta"
    DETECTED_UNVERIFIED = "detected_unverified"


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


def _latest_harness_session_id(record: session_store.SessionRecord) -> str | None:
    for candidate in reversed(record.harness_session_ids):
        normalized = candidate.strip()
        if normalized:
            return normalized
    return _normalize(record.harness_session_id)


def _primary_spawn_for_chat(
    project_root: Path,
    runtime_root: Path,
    chat_id: str,
) -> SpawnRecord | None:
    _ = project_root
    spawns = spawn_store.list_spawns(runtime_root, filters={"chat_id": chat_id})
    primary_spawns = [row for row in spawns if row.kind == "primary"]
    if not primary_spawns:
        return None
    return primary_spawns[-1]


def _started_at_observation_window(started_at: str | None) -> tuple[float | None, str | None]:
    from datetime import datetime

    normalized_started_at = (started_at or "").strip()
    if not normalized_started_at:
        return (None, None)
    if normalized_started_at.endswith("Z"):
        normalized_started_at = f"{normalized_started_at[:-1]}+00:00"
    try:
        parsed_started_at = datetime.fromisoformat(normalized_started_at)
    except ValueError:
        return (None, None)
    started_at_epoch = parsed_started_at.timestamp()
    started_at_local_iso = datetime.fromtimestamp(started_at_epoch).strftime("%Y-%m-%dT%H:%M:%S")
    return (started_at_epoch, started_at_local_iso)


def _detect_primary_harness_session_id(
    *,
    project_root: Path,
    spawn_row: SpawnRecord,
    harness_hint: str | None,
) -> str | None:
    if spawn_row.kind != "primary":
        return None
    normalized_harness = (harness_hint or spawn_row.harness or "").strip().lower()
    if not normalized_harness:
        return None
    started_at_epoch, started_at_local_iso = _started_at_observation_window(spawn_row.started_at)
    if started_at_epoch is None:
        return None

    registry = get_default_harness_registry()
    try:
        harness_id = HarnessId(normalized_harness)
        adapter = registry.get_subprocess_harness(harness_id)
    except (KeyError, TypeError, ValueError):
        return None

    detected_harness_session_id = (
        adapter.detect_primary_session_id(
            project_root=project_root,
            started_at_epoch=started_at_epoch,
            started_at_local_iso=started_at_local_iso,
        )
        or ""
    ).strip()
    if not detected_harness_session_id:
        return None

    return detected_harness_session_id


def _recover_from_session_store(runtime_root: Path, chat_id: str) -> RecoveryResult | None:
    records = session_store.get_session_records(runtime_root, {chat_id})
    if not records:
        return None
    session = records[0]
    session_id = _latest_harness_session_id(session)
    if session_id:
        return RecoveryResult(
            harness_session_id=session_id,
            provenance=RecoveryProvenance.SESSION_STORE,
            supporting_chat_id=chat_id,
        )
    return None


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


def _recover_from_primary_meta(
    runtime_root: Path, spawn_id: str, chat_id: str | None
) -> RecoveryResult | None:
    session_id = primary_meta.read_primary_harness_session_id(runtime_root, spawn_id)
    if session_id:
        return RecoveryResult(
            harness_session_id=session_id,
            provenance=RecoveryProvenance.PRIMARY_META,
            supporting_chat_id=chat_id,
        )
    return None


def _recover_from_detection(
    *,
    project_root: Path,
    runtime_root: Path,
    chat_id: str | None,
    harness_hint: str | None,
) -> RecoveryResult | None:
    if chat_id is None:
        return None
    primary_spawn = _primary_spawn_for_chat(project_root, runtime_root, chat_id)
    if primary_spawn is None:
        return None
    detected = _detect_primary_harness_session_id(
        project_root=project_root,
        spawn_row=primary_spawn,
        harness_hint=harness_hint,
    )
    if detected:
        return RecoveryResult(
            harness_session_id=detected,
            provenance=RecoveryProvenance.DETECTED_UNVERIFIED,
            supporting_chat_id=chat_id,
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
    """Attempt to recover a harness session ID for a tracked reference.

    Only runs when the recorded ID is missing or empty. Tries recovery
    sources in order of authority:

    1. SESSION_STORE - from session records (for chat refs)
    2. SPAWN_ROW - from spawn record directly
    3. PRIMARY_META - from primary spawn metadata
    4. DETECTED_UNVERIFIED - from harness adapter detection

    Args:
        project_root: Project root path.
        runtime_root: Runtime state root path.
        ref: The reference (chat ID like c123, spawn ID like p123, or UUID).
        recorded_harness_session_id: Already-recorded session ID, if any.
        recorded_harness: Already-recorded harness name, if any.

    Returns:
        RecoveryResult with the recovered ID and provenance, or None if
        no recovery is possible.
    """
    if recorded_harness_session_id and recorded_harness_session_id.strip():
        return None

    normalized_ref = ref.strip()
    if not normalized_ref:
        return None

    # Chat reference
    if normalized_ref.startswith("c") and normalized_ref[1:].isdigit():
        result = _recover_from_session_store(runtime_root, normalized_ref)
        if result is not None:
            return result

        primary_spawn = _primary_spawn_for_chat(project_root, runtime_root, normalized_ref)
        if primary_spawn is not None:
            result = _recover_from_primary_meta(runtime_root, primary_spawn.id, normalized_ref)
            if result is not None:
                return result

        return _recover_from_detection(
            project_root=project_root,
            runtime_root=runtime_root,
            chat_id=normalized_ref,
            harness_hint=recorded_harness,
        )

    # Spawn reference
    if normalized_ref.startswith("p") and normalized_ref[1:].isdigit():
        result = _recover_from_spawn_row(runtime_root, normalized_ref)
        if result is not None:
            return result

        row = spawn_store.get_spawn(runtime_root, normalized_ref)
        if row is not None and row.chat_id:
            result = _recover_from_session_store(runtime_root, row.chat_id)
            if result is not None:
                return result

            result = _recover_from_primary_meta(runtime_root, normalized_ref, row.chat_id)
            if result is not None:
                return result

        return _recover_from_detection(
            project_root=project_root,
            runtime_root=runtime_root,
            chat_id=row.chat_id if row else None,
            harness_hint=recorded_harness,
        )

    return None


__all__ = [
    "RecoveryProvenance",
    "RecoveryResult",
    "recover_harness_session_id",
]
