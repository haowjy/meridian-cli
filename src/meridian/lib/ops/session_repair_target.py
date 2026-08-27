"""Session-reference repair target resolution."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
from pathlib import Path
from typing import NamedTuple

from meridian.lib.harness.session_detection import infer_harness_from_untracked_session_ref
from meridian.lib.ops.session_target import (
    _config_root_hint,
    _detect_primary_session_id,
    _is_chat_ref,
    _is_spawn_ref,
    _latest_harness_session_id,
    _primary_transcript_unavailable_message,
    _read_chat_session_record,
    _resolve_harness_transcript_target_or_none,
    _resolve_transcript_from_candidates,
)
from meridian.lib.ops.spawn.query import (
    read_latest_primary_spawn_for_chat_read_only,
    read_spawn_row_read_only,
)
from meridian.lib.state import session_identity
from meridian.lib.state.primary_meta import read_primary_harness_session_id


class SessionRepairTarget(NamedTuple):
    detected_harness_session_id: str | None
    source: str | None
    reason: str | None = None


def _repair_target_from_detected_session_id(
    *,
    project_root: Path,
    harness: str | None,
    detected_session_id: str,
    config_root_hint: Path | None,
    invalid_reason: str,
    unavailable_reason: str,
) -> SessionRepairTarget:
    if _is_spawn_ref(detected_session_id):
        return SessionRepairTarget(
            detected_harness_session_id=None,
            source=None,
            reason=invalid_reason,
        )

    transcript_target = _resolve_harness_transcript_target_or_none(
        project_root=project_root,
        session_id=detected_session_id,
        harness=harness,
        config_root_hint=config_root_hint,
    )
    if transcript_target is not None:
        return SessionRepairTarget(
            detected_harness_session_id=transcript_target.session_id,
            source=transcript_target.source,
        )

    return SessionRepairTarget(
        detected_harness_session_id=detected_session_id,
        source=f"{harness or 'primary'} detected session id",
        reason=unavailable_reason,
    )

def _resolve_repair_from_chat_id(
    *,
    project_root: Path,
    runtime_root: Path,
    chat_id: str,
) -> SessionRepairTarget:
    session_record = _read_chat_session_record(runtime_root, chat_id)
    if session_record is None:
        raise ValueError(f"Chat '{chat_id}' not found")

    primary_spawn = session_identity.get_recorded_primary_spawn_for_owner_chat(
        runtime_root,
        chat_id,
        session_record.spawn_id,
    )
    if primary_spawn is None:
        primary_spawn = read_latest_primary_spawn_for_chat_read_only(
            project_root,
            chat_id,
            runtime_root=runtime_root,
        )
    harness = session_record.harness.strip() or None
    if harness is None and primary_spawn is not None:
        harness = (primary_spawn.harness or "").strip() or None
    config_root_hint = _config_root_hint(
        session_record.claude_config_dir
        or (primary_spawn.claude_config_dir if primary_spawn is not None else None)
    )

    transcript_target = _resolve_transcript_from_candidates(
        project_root=project_root,
        harness=harness,
        candidate_ids=[
            _latest_harness_session_id(session_record),
            (
                read_primary_harness_session_id(runtime_root, primary_spawn.id)
                if primary_spawn is not None
                else None
            ),
        ],
        config_root_hint=config_root_hint,
    )
    if transcript_target is not None:
        return SessionRepairTarget(
            detected_harness_session_id=transcript_target.session_id,
            source=transcript_target.source,
        )

    detected_session_id = _detect_primary_session_id(
        project_root=project_root,
        runtime_root=runtime_root,
        spawn_row=primary_spawn,
        harness=harness,
    )
    if detected_session_id is not None:
        return _repair_target_from_detected_session_id(
            project_root=project_root,
            harness=harness,
            detected_session_id=detected_session_id,
            config_root_hint=config_root_hint,
            invalid_reason=(
                f"Detected session id '{detected_session_id}' for chat '{chat_id}' "
                "looks like a spawn id; refusing repair."
            ),
            unavailable_reason=(
                f"Detected harness session id '{detected_session_id}' for chat '{chat_id}' "
                "but transcript file is not available yet."
            ),
        )

    return SessionRepairTarget(
        detected_harness_session_id=None,
        source=None,
        reason=_primary_transcript_unavailable_message(chat_id),
    )


def _resolve_repair_from_spawn_id(
    *,
    project_root: Path,
    runtime_root: Path,
    spawn_id: str,
) -> SessionRepairTarget:
    row = read_spawn_row_read_only(project_root, spawn_id, runtime_root=runtime_root)
    if row is None:
        raise ValueError(f"Spawn '{spawn_id}' not found")

    harness = (row.harness or "").strip() or None
    config_root_hint = _config_root_hint(row.claude_config_dir)
    row_session_id = (row.harness_session_id or "").strip()
    transcript_target = _resolve_transcript_from_candidates(
        project_root=project_root,
        harness=harness,
        candidate_ids=[row_session_id if not _is_spawn_ref(row_session_id) else None],
        config_root_hint=config_root_hint,
    )
    if transcript_target is not None:
        return SessionRepairTarget(
            detected_harness_session_id=transcript_target.session_id,
            source=transcript_target.source,
        )

    if row.kind != "primary":
        return SessionRepairTarget(
            detected_harness_session_id=None,
            source=None,
            reason=(
                f"Spawn '{spawn_id}' has no detected harness session id to repair "
                "(non-primary spawns may only have output fallback)."
            ),
        )

    if row.chat_id:
        chat_record = _read_chat_session_record(runtime_root, row.chat_id)
        if chat_record is not None:
            if harness is None:
                harness = chat_record.harness.strip() or None
            if config_root_hint is None:
                config_root_hint = _config_root_hint(chat_record.claude_config_dir)
            transcript_target = _resolve_transcript_from_candidates(
                project_root=project_root,
                harness=harness,
                candidate_ids=[_latest_harness_session_id(chat_record)],
                config_root_hint=config_root_hint,
            )
            if transcript_target is not None:
                return SessionRepairTarget(
                    detected_harness_session_id=transcript_target.session_id,
                    source=transcript_target.source,
                )

    transcript_target = _resolve_transcript_from_candidates(
        project_root=project_root,
        harness=harness,
        candidate_ids=[
            (
                read_primary_harness_session_id(runtime_root, spawn_id)
                if row.kind == "primary"
                else None
            )
        ],
        config_root_hint=config_root_hint,
    )
    if transcript_target is not None:
        return SessionRepairTarget(
            detected_harness_session_id=transcript_target.session_id,
            source=transcript_target.source,
        )

    detected_session_id = _detect_primary_session_id(
        project_root=project_root,
        runtime_root=runtime_root,
        spawn_row=row,
        harness=harness,
    )
    if detected_session_id is None:
        return SessionRepairTarget(
            detected_harness_session_id=None,
            source=None,
            reason=(
                f"Spawn '{spawn_id}' has no detected harness session id to repair "
                "(no harness transcript available yet)."
            ),
        )

    return _repair_target_from_detected_session_id(
        project_root=project_root,
        harness=harness,
        detected_session_id=detected_session_id,
        config_root_hint=config_root_hint,
        invalid_reason=(
            f"Detected session id '{detected_session_id}' for spawn "
            f"'{spawn_id}' looks like a spawn id; refusing repair."
        ),
        unavailable_reason=(
            f"Detected harness session id '{detected_session_id}' for "
            f"spawn '{spawn_id}' but transcript file is not available yet."
        ),
    )


def _resolve_repair_from_session_ref(
    *,
    project_root: Path,
    session_ref: str,
) -> SessionRepairTarget:
    inferred = infer_harness_from_untracked_session_ref(project_root, session_ref)
    harness = str(inferred) if inferred is not None else None
    transcript_target = _resolve_harness_transcript_target_or_none(
        project_root=project_root,
        session_id=session_ref,
        harness=harness,
        config_root_hint=None,
    )
    if transcript_target is not None:
        return SessionRepairTarget(
            detected_harness_session_id=transcript_target.session_id,
            source=transcript_target.source,
        )
    raise FileNotFoundError(f"Session file for '{session_ref}' not found")


def resolve_session_repair_target(
    *,
    ref: str,
    project_root: Path,
    runtime_root: Path,
) -> SessionRepairTarget:
    normalized_ref = ref.strip()
    if not normalized_ref:
        raise ValueError("Session reference is required")
    if _is_chat_ref(runtime_root, normalized_ref):
        return _resolve_repair_from_chat_id(
            project_root=project_root,
            runtime_root=runtime_root,
            chat_id=normalized_ref,
        )
    if _is_spawn_ref(normalized_ref):
        return _resolve_repair_from_spawn_id(
            project_root=project_root,
            runtime_root=runtime_root,
            spawn_id=normalized_ref,
        )
    return _resolve_repair_from_session_ref(
        project_root=project_root,
        session_ref=normalized_ref,
    )


__all__ = ["SessionRepairTarget", "resolve_session_repair_target"]
