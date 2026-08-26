"""Read-only harness session ID recovery for tracked references.

When a tracked spawn or chat reference lacks a recorded harness session ID,
this module provides fallback recovery from durable state and harness
adapters. It does NOT persist recovered IDs; callers decide whether to
write back based on their own verification policies.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.extractors.pi import detect_pi_session_id_from_session_files
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.state import primary_meta, session_identity, session_store, spawn_store
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
    runtime_root: Path,
    chat_id: str,
    spawn_id: str | None = None,
) -> SpawnRecord | None:
    normalized_spawn_id = (spawn_id or "").strip()
    if normalized_spawn_id:
        direct = spawn_store.get_spawn(runtime_root, normalized_spawn_id)
        if (
            direct is not None
            and direct.kind == "primary"
            and session_identity.spawn_owner_chat_id(direct) == chat_id
        ):
            return direct
    spawns = session_identity.list_spawns_for_owner_chat(runtime_root, chat_id)
    primary_spawns = [row for row in spawns.records if row.kind == "primary"]
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
    runtime_root: Path,
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

    metadata = primary_meta.read_primary_metadata(runtime_root, spawn_row.id)
    detection_cwd = (
        (spawn_row.execution_cwd or "").strip()
        or (spawn_row.task_cwd or "").strip()
        or ((metadata.launch_cwd or "").strip() if metadata is not None else "")
        or str(project_root)
    )
    resolved_detection_cwd = Path(detection_cwd).expanduser()

    registry = get_default_harness_registry()
    try:
        harness_id = HarnessId(normalized_harness)
        adapter = registry.get_subprocess_harness(harness_id)
    except (KeyError, TypeError, ValueError):
        return None

    expected_session_id = (spawn_row.harness_session_id or "").strip() or None
    if harness_id is HarnessId.PI:
        launch_env: dict[str, str] = {}
        session_dir = (metadata.session_dir or "").strip() if metadata is not None else ""
        if session_dir:
            launch_env["PI_CODING_AGENT_SESSION_DIR"] = session_dir
        detected = detect_pi_session_id_from_session_files(
            launch_env=launch_env,
            child_cwd=resolved_detection_cwd,
            started_at_epoch=started_at_epoch,
            expected_session_id=expected_session_id,
        )
        normalized_detected = _normalize(detected)
        if normalized_detected:
            return normalized_detected

    detected_harness_session_id = (
        adapter.detect_primary_session_id(
            project_root=resolved_detection_cwd,
            started_at_epoch=started_at_epoch,
            started_at_local_iso=started_at_local_iso,
            expected_session_id=expected_session_id,
        )
        or ""
    ).strip()
    if not detected_harness_session_id:
        return None

    return detected_harness_session_id


def recover_recorded_chat_harness_session_id(
    runtime_root: Path,
    chat_id: str,
    *,
    session: session_store.SessionRecord | None = None,
) -> RecoveryResult | None:
    """Resolve a chat's durable harness ID without native transcript detection."""

    resolved_session = session
    if resolved_session is None:
        records = session_store.get_session_records(runtime_root, {chat_id})
        resolved_session = records[0] if records else None
    if resolved_session is not None:
        recovered = _recover_from_session_record(resolved_session)
        if recovered is not None:
            return recovered

    recorded_spawn_id = resolved_session.spawn_id if resolved_session is not None else None
    if not recorded_spawn_id and session_store.primary_spawn_backfill_complete(runtime_root):
        return None
    primary_spawn = _primary_spawn_for_chat(
        runtime_root,
        chat_id,
        recorded_spawn_id,
    )
    if primary_spawn is None:
        return None
    return _recover_from_primary_spawn(runtime_root, primary_spawn, chat_id)


def recover_recorded_chat_harness_session_ids(
    runtime_root: Path,
    sessions: Sequence[session_store.SessionRecord],
) -> dict[str, RecoveryResult]:
    """Resolve durable harness IDs for many chats with one spawn-state scan."""

    results: dict[str, RecoveryResult] = {}
    unresolved: set[str] = set()
    for session in sessions:
        recovered = _recover_from_session_record(session)
        if recovered is not None:
            results[session.chat_id] = recovered
            continue
        normalized_spawn_id = (session.spawn_id or "").strip()
        if normalized_spawn_id:
            spawn = spawn_store.get_spawn(runtime_root, normalized_spawn_id)
            if (
                spawn is not None
                and spawn.kind == "primary"
                and session_identity.spawn_owner_chat_id(spawn) == session.chat_id
            ):
                recovered = _recover_from_primary_spawn(
                    runtime_root,
                    spawn,
                    session.chat_id,
                )
                if recovered is not None:
                    results[session.chat_id] = recovered
                    continue
        unresolved.add(session.chat_id)
    if not unresolved:
        return results
    unresolved_sessions = {
        session.chat_id: session for session in sessions if session.chat_id in unresolved
    }
    if all(not session.spawn_id for session in unresolved_sessions.values()) and (
        session_store.primary_spawn_backfill_complete(runtime_root)
    ):
        return results

    primary_spawns: dict[str, SpawnRecord] = {}
    for spawn in spawn_store.list_spawns(runtime_root).records:
        owner_chat_id = session_identity.spawn_owner_chat_id(spawn)
        if spawn.kind == "primary" and owner_chat_id in unresolved:
            primary_spawns[owner_chat_id] = spawn

    for chat_id, spawn in primary_spawns.items():
        recovered = _recover_from_primary_spawn(runtime_root, spawn, chat_id)
        if recovered is not None:
            results[chat_id] = recovered
    return results


def _recover_from_session_record(
    session: session_store.SessionRecord,
) -> RecoveryResult | None:
    session_id = _latest_harness_session_id(session)
    if session_id is None:
        return None
    return RecoveryResult(
        harness_session_id=session_id,
        provenance=RecoveryProvenance.SESSION_STORE,
        supporting_chat_id=session.chat_id,
    )


def _recover_from_primary_spawn(
    runtime_root: Path,
    spawn: SpawnRecord,
    chat_id: str,
) -> RecoveryResult | None:
    session_id = _normalize(spawn.harness_session_id)
    if session_id is not None:
        return RecoveryResult(
            harness_session_id=session_id,
            provenance=RecoveryProvenance.SPAWN_ROW,
            supporting_chat_id=chat_id,
        )
    return _recover_from_primary_meta(runtime_root, spawn.id, chat_id)


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
    primary_spawn = _primary_spawn_for_chat(runtime_root, chat_id)
    if primary_spawn is None:
        return None
    detected = _detect_primary_harness_session_id(
        project_root=project_root,
        runtime_root=runtime_root,
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
        result = recover_recorded_chat_harness_session_id(runtime_root, normalized_ref)
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
        if row is not None:
            linked_record = session_identity.get_session_record_for_spawn(
                runtime_root,
                normalized_ref,
                require_harness_session_id=True,
            )
            if linked_record is not None:
                session_id = _latest_harness_session_id(linked_record)
                if session_id:
                    return RecoveryResult(
                        harness_session_id=session_id,
                        provenance=RecoveryProvenance.SESSION_STORE,
                        supporting_chat_id=linked_record.chat_id,
                    )

            if row.kind == "primary":
                result = _recover_from_primary_meta(runtime_root, normalized_ref, row.chat_id)
                if result is not None:
                    return result

                return _recover_from_detection(
                    project_root=project_root,
                    runtime_root=runtime_root,
                    chat_id=row.chat_id,
                    harness_hint=recorded_harness,
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
