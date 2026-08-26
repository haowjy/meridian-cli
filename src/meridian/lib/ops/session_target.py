"""Session-log target resolution helpers.

This module resolves user refs (chat, spawn, harness session id, or explicit file)
into a concrete transcript file target. It is intentionally read-only: no state
mutation or repair writes happen during resolution.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal, NamedTuple

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.adapter import SubprocessHarness
from meridian.lib.harness.opencode_transcript import opencode_db_session_exists
from meridian.lib.harness.pi_paths import resolve_pi_spawn_session_root
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.harness.session_detection import infer_harness_from_untracked_session_ref
from meridian.lib.ops.spawn.query import (
    read_latest_primary_spawn_for_chat_read_only,
    read_spawn_row_read_only,
)
from meridian.lib.state import session_identity, session_store, spawn_store
from meridian.lib.state.paths import resolve_spawn_output_path
from meridian.lib.state.primary_meta import (
    is_managed_primary,
    read_primary_harness_session_id,
    read_primary_metadata,
)
from meridian.lib.state.spawn.model import SpawnRecord

_CODEX_FILENAME_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(?P<session_id>[0-9a-fA-F-]{36})\.jsonl$"
)
_PRIMARY_TRANSCRIPT_UNAVAILABLE_SUFFIX = (
    "exists but no transcript is available yet (no harness session id recorded)."
)


class TranscriptSource(NamedTuple):
    kind: Literal["file", "opencode_db", "spawn_history"]
    session_id: str
    harness: str | None
    source_label: str
    path: Path | None = None


class SessionLogTarget(NamedTuple):
    session_id: str
    harness: str | None
    file_path: Path | None
    source: str
    sources: tuple[TranscriptSource, ...]


class ChatSessionLogTargetResolution(NamedTuple):
    chat_id: str
    target: SessionLogTarget | None
    error: str | None = None


def _is_chat_ref(runtime_root: Path, value: str) -> bool:
    return session_identity.is_tracked_chat_ref(runtime_root, value)


def _is_spawn_ref(value: str) -> bool:
    return value.startswith("p") and value[1:].isdigit()


def _extract_session_id_from_path(path: Path) -> str:
    if path.suffix == ".jsonl" and path.stem:
        codex_match = _CODEX_FILENAME_RE.match(path.name)
        if codex_match is not None:
            return codex_match.group("session_id")
        return path.stem
    return path.name


def _target_from_source(source: TranscriptSource) -> SessionLogTarget:
    return SessionLogTarget(
        session_id=source.session_id,
        harness=source.harness,
        file_path=source.path,
        source=source.source_label,
        sources=(source,),
    )


def _source_key(source: TranscriptSource) -> tuple[str, str, str | None, str]:
    return (
        source.kind,
        source.session_id,
        source.path.as_posix() if source.path is not None else None,
        source.source_label,
    )


def _with_sources(
    target: SessionLogTarget,
    *additional_targets: SessionLogTarget | None,
) -> SessionLogTarget:
    sources = list(target.sources)
    seen = {_source_key(source) for source in sources}
    for additional_target in additional_targets:
        if additional_target is None:
            continue
        for source in additional_target.sources:
            key = _source_key(source)
            if key in seen:
                continue
            sources.append(source)
            seen.add(key)
    return target._replace(sources=tuple(sources))


def _resolve_file_target(file_path: str) -> SessionLogTarget:
    resolved = Path(file_path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Session file '{resolved.as_posix()}' not found")

    harness: str | None = None
    parts = set(resolved.parts)
    if ".claude" in parts:
        harness = "claude"
    elif ".codex" in parts:
        harness = "codex"

    return _target_from_source(
        TranscriptSource(
            kind="file",
            session_id=_extract_session_id_from_path(resolved),
            harness=harness,
            path=resolved,
            source_label="file",
        )
    )


def _resolve_adapter_file_target(
    *,
    project_root: Path,
    session_id: str,
    harness_id: HarnessId,
    adapter: SubprocessHarness,
    config_root_hint: Path | None,
) -> SessionLogTarget | None:
    candidate = adapter.resolve_session_file(
        project_root=project_root,
        session_id=session_id,
        config_root_hint=config_root_hint,
    )
    if candidate is None or not candidate.is_file():
        return None
    return _target_from_source(
        TranscriptSource(
            kind="file",
            session_id=session_id,
            harness=str(harness_id),
            path=candidate,
            source_label=f"{harness_id} transcript",
        )
    )


def _opencode_db_target(*, session_id: str) -> SessionLogTarget:
    return _target_from_source(
        TranscriptSource(
            kind="opencode_db",
            session_id=session_id,
            harness=HarnessId.OPENCODE.value,
            source_label="opencode transcript",
        )
    )


def _resolve_harness_session_file(
    *,
    project_root: Path,
    session_id: str,
    harness: str | None,
    config_root_hint: Path | None,
) -> SessionLogTarget:
    normalized_session_id = session_id.strip()
    if not normalized_session_id:
        raise FileNotFoundError("Session ID is required to resolve harness session file")

    registry = get_default_harness_registry()
    normalized_harness = (harness or "").strip().lower() or None
    if normalized_harness is not None:
        try:
            harness_id = HarnessId(normalized_harness)
            adapter = registry.get_subprocess_harness(harness_id)
        except (ValueError, KeyError, TypeError) as exc:
            raise FileNotFoundError(
                f"Session file for '{normalized_session_id}' "
                f"(harness={normalized_harness}) not found"
            ) from exc

        file_target = _resolve_adapter_file_target(
            project_root=project_root,
            session_id=normalized_session_id,
            harness_id=harness_id,
            adapter=adapter,
            config_root_hint=config_root_hint,
        )
        if (
            harness_id == HarnessId.OPENCODE
            and opencode_db_session_exists(session_id=normalized_session_id)
        ):
            return _with_sources(
                _opencode_db_target(session_id=normalized_session_id),
                file_target,
            )
        if file_target is not None:
            return file_target
        raise FileNotFoundError(
            f"Session file for '{normalized_session_id}' (harness={normalized_harness}) not found"
        )

    checked_harnesses: list[str] = []
    for harness_id in registry.ids():
        try:
            adapter = registry.get_subprocess_harness(harness_id)
        except TypeError:
            continue
        checked_harnesses.append(str(harness_id))
        file_target = _resolve_adapter_file_target(
            project_root=project_root,
            session_id=normalized_session_id,
            harness_id=harness_id,
            adapter=adapter,
            config_root_hint=config_root_hint,
        )
        if (
            harness_id == HarnessId.OPENCODE
            and opencode_db_session_exists(session_id=normalized_session_id)
        ):
            return _with_sources(
                _opencode_db_target(session_id=normalized_session_id),
                file_target,
            )
        if file_target is not None:
            return file_target

    checked = ", ".join(checked_harnesses) if checked_harnesses else "<none>"
    raise FileNotFoundError(
        f"Session file for '{normalized_session_id}' not found. Checked harnesses: {checked}"
    )


def _resolve_harness_transcript_target_or_none(
    *,
    project_root: Path,
    session_id: str,
    harness: str | None,
    config_root_hint: Path | None,
) -> SessionLogTarget | None:
    try:
        return _resolve_harness_session_file(
            project_root=project_root,
            session_id=session_id,
            harness=harness,
            config_root_hint=config_root_hint,
        )
    except FileNotFoundError:
        return None


def _primary_transcript_unavailable_message(ref: str) -> str:
    return f"Session '{ref}' {_PRIMARY_TRANSCRIPT_UNAVAILABLE_SUFFIX}"


def _started_at_observation_window(started_at: str | None) -> tuple[float | None, str | None]:
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


def spawn_output_path_for_target(
    runtime_root: Path,
    spawn_id: str,
) -> Path | None:
    return resolve_spawn_output_path(runtime_root, spawn_id)


def _managed_primary_fallback_source(spawn_id: str, harness: str | None) -> str:
    source = f"spawn {spawn_id} output"
    normalized_harness = (harness or "").strip().lower()
    if normalized_harness == "opencode":
        return f"{source} (best-effort fallback; native opencode transcript unavailable)"
    return source


def _target_from_spawn_output(
    runtime_root: Path,
    *,
    display_id: str,
    spawn_id: str,
    source: str | None = None,
) -> SessionLogTarget | None:
    output_path = spawn_output_path_for_target(runtime_root, spawn_id)
    if output_path is None:
        return None
    return _target_from_source(
        TranscriptSource(
            kind="spawn_history",
            session_id=display_id,
            harness=None,
            path=output_path,
            source_label=source or f"spawn {spawn_id} output",
        )
    )


def _managed_primary_output_target(
    runtime_root: Path,
    *,
    spawn_row: SpawnRecord,
    display_id: str,
    harness: str | None,
) -> SessionLogTarget | None:
    if spawn_row.kind != "primary":
        return None
    if not is_managed_primary(runtime_root, spawn_row.id):
        return None
    return _target_from_spawn_output(
        runtime_root,
        display_id=display_id,
        spawn_id=spawn_row.id,
        source=_managed_primary_fallback_source(spawn_row.id, harness),
    )


def _running_managed_primary_output_target(
    runtime_root: Path,
    *,
    spawn_row: SpawnRecord | None,
    display_id: str,
) -> SessionLogTarget | None:
    if spawn_row is None:
        return None
    if spawn_row.kind != "primary":
        return None
    if spawn_row.status not in {"queued", "running"}:
        return None
    if not is_managed_primary(runtime_root, spawn_row.id):
        return None
    return _target_from_spawn_output(
        runtime_root,
        display_id=display_id,
        spawn_id=spawn_row.id,
    )


def _resolved_spawn_output_fallback_target(
    *,
    runtime_root: Path,
    row: SpawnRecord,
    display_id: str,
    is_primary_spawn: bool,
    is_managed_backend_primary: bool,
    harness: str | None,
) -> SessionLogTarget | None:
    if is_primary_spawn:
        if not is_managed_backend_primary:
            return None
        return _managed_primary_output_target(
            runtime_root,
            spawn_row=row,
            display_id=display_id,
            harness=harness,
        )
    return _target_from_spawn_output(
        runtime_root,
        display_id=display_id,
        spawn_id=row.id,
    )


def _spawn_history_fallback_for_session_ref(
    *,
    project_root: Path,
    runtime_root: Path,
    display_id: str,
    record: session_store.SessionRecord,
) -> SessionLogTarget | None:
    spawn_id = (record.spawn_id or "").strip()
    if spawn_id:
        row = read_spawn_row_read_only(project_root, spawn_id, runtime_root=runtime_root)
        if row is not None:
            output_target = _target_from_spawn_output(
                runtime_root,
                display_id=display_id,
                spawn_id=row.id,
            )
            if output_target is not None:
                return output_target

    return _spawn_history_fallback_for_harness_session_id(
        runtime_root=runtime_root,
        display_id=display_id,
        harness_session_id=record.harness_session_id or "",
    )


def _spawn_history_fallback_for_harness_session_id(
    *,
    runtime_root: Path,
    display_id: str,
    harness_session_id: str,
) -> SessionLogTarget | None:
    normalized_session_id = harness_session_id.strip()
    if not normalized_session_id:
        return None
    for row in reversed(spawn_store.list_spawns(runtime_root).records):
        if (row.harness_session_id or "").strip() != normalized_session_id:
            continue
        output_target = _target_from_spawn_output(
            runtime_root,
            display_id=display_id,
            spawn_id=row.id,
        )
        if output_target is not None:
            return output_target
    return None


def _spawn_history_fallback_for_chat_ref(
    *,
    runtime_root: Path,
    display_id: str,
    chat_id: str,
    primary_spawn: SpawnRecord | None,
    related_spawns: Sequence[SpawnRecord] | None = None,
) -> SessionLogTarget | None:
    if primary_spawn is not None:
        output_target = _target_from_spawn_output(
            runtime_root,
            display_id=display_id,
            spawn_id=primary_spawn.id,
        )
        if output_target is not None:
            return output_target

    candidates: list[SpawnRecord] = []
    if related_spawns is None:
        candidates.extend(
            reversed(
                session_identity.list_spawns_for_exact_session(runtime_root, chat_id).records
            )
        )
        candidates.extend(
            reversed(session_identity.list_spawns_for_owner_chat(runtime_root, chat_id).records)
        )
    else:
        candidates.extend(
            row for row in reversed(related_spawns) if row.chat_id == chat_id
        )
        candidates.extend(
            row
            for row in reversed(related_spawns)
            if session_identity.spawn_owner_chat_id(row) == chat_id
        )

    seen: set[str] = set()
    for row in candidates:
        if row.id in seen:
            continue
        seen.add(row.id)
        output_target = _target_from_spawn_output(
            runtime_root,
            display_id=display_id,
            spawn_id=row.id,
        )
        if output_target is not None:
            return output_target
    return None


def _primary_spawn_for_chat(
    project_root: Path,
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
        return None
    return read_latest_primary_spawn_for_chat_read_only(
        project_root,
        chat_id,
        runtime_root=runtime_root,
    )


def _latest_harness_session_id(record: session_store.SessionRecord) -> str | None:
    for candidate in reversed(record.harness_session_ids):
        normalized = candidate.strip()
        if normalized:
            return normalized
    normalized = (record.harness_session_id or "").strip()
    return normalized or None


def _config_root_hint(value: str | None) -> Path | None:
    normalized = (value or "").strip()
    return Path(normalized).expanduser() if normalized else None


def _read_chat_session_record(
    runtime_root: Path, chat_id: str
) -> session_store.SessionRecord | None:
    records = session_store.get_session_records(runtime_root, {chat_id})
    if not records:
        return None
    return records[0]


def _resolve_transcript_from_candidates(
    *,
    project_root: Path,
    harness: str | None,
    candidate_ids: list[str | None],
    config_root_hint: Path | None,
) -> SessionLogTarget | None:
    seen: set[str] = set()
    for candidate_id in candidate_ids:
        normalized_candidate_id = (candidate_id or "").strip()
        if not normalized_candidate_id or normalized_candidate_id in seen:
            continue
        seen.add(normalized_candidate_id)
        transcript_target = _resolve_harness_transcript_target_or_none(
            project_root=project_root,
            session_id=normalized_candidate_id,
            harness=harness,
            config_root_hint=config_root_hint,
        )
        if transcript_target is not None:
            return transcript_target
    return None


def _detect_primary_session_id(
    *,
    project_root: Path,
    runtime_root: Path,
    spawn_row: SpawnRecord | None,
    harness: str | None,
) -> str | None:
    if spawn_row is None:
        return None
    if _skip_primary_default_root_detection(
        runtime_root=runtime_root,
        spawn_row=spawn_row,
        harness=harness,
    ):
        return None
    detected_session_id = _detect_primary_harness_session_id(
        project_root=project_root,
        spawn_row=spawn_row,
        harness_hint=harness,
    )
    normalized_detected_session_id = (detected_session_id or "").strip()
    return normalized_detected_session_id or None


def _normalized_path_text(path: str | Path) -> str | None:
    try:
        normalized = Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        try:
            normalized = Path(path).expanduser().absolute()
        except (OSError, RuntimeError, ValueError):
            return None
    return normalized.as_posix().rstrip("/").lower()


def _skip_primary_default_root_detection(
    *,
    runtime_root: Path,
    spawn_row: SpawnRecord,
    harness: str | None,
) -> bool:
    if spawn_row.kind != "primary":
        return False
    normalized_harness = (harness or spawn_row.harness or "").strip().lower()
    if normalized_harness != HarnessId.PI.value:
        return False
    metadata = read_primary_metadata(runtime_root, spawn_row.id)
    if metadata is None:
        return False
    if metadata.harness_session_discovery == "never_created":
        return True
    session_dir = (metadata.session_dir or "").strip()
    if not session_dir:
        return False
    configured_session_dir = _normalized_path_text(session_dir)
    default_session_dir = _normalized_path_text(resolve_pi_spawn_session_root())
    if configured_session_dir is None or default_session_dir is None:
        return False
    return configured_session_dir != default_session_dir


def _resolve_from_chat_state(
    *,
    project_root: Path,
    runtime_root: Path,
    chat_id: str,
    session_record: session_store.SessionRecord,
    primary_spawn: SpawnRecord | None,
    related_spawns: Sequence[SpawnRecord] | None = None,
) -> SessionLogTarget:
    normalized_harness = session_record.harness.strip() or None
    if normalized_harness is None and primary_spawn is not None and primary_spawn.harness:
        normalized_harness = primary_spawn.harness.strip() or None
    config_root_hint = _config_root_hint(
        session_record.claude_config_dir
        or (primary_spawn.claude_config_dir if primary_spawn is not None else None)
    )

    output_target = _running_managed_primary_output_target(
        runtime_root,
        spawn_row=primary_spawn,
        display_id=chat_id,
    )
    if output_target is not None:
        return output_target

    normalized_session_id = _latest_harness_session_id(session_record)
    if normalized_session_id is None and primary_spawn is not None:
        normalized_session_id = (
            read_primary_harness_session_id(runtime_root, primary_spawn.id) or ""
        ).strip() or None

    if normalized_session_id is None:
        if primary_spawn is None:
            raise ValueError(_primary_transcript_unavailable_message(chat_id))
        normalized_session_id = _detect_primary_session_id(
            project_root=project_root,
            runtime_root=runtime_root,
            spawn_row=primary_spawn,
            harness=normalized_harness,
        )
        if normalized_session_id is None:
            raise ValueError(_primary_transcript_unavailable_message(chat_id))

    if not normalized_session_id.strip():
        raise ValueError(f"Chat '{chat_id}' not found")

    if normalized_harness is None:
        inferred = infer_harness_from_untracked_session_ref(project_root, normalized_session_id)
        normalized_harness = str(inferred) if inferred is not None else None

    transcript_target = _resolve_transcript_from_candidates(
        project_root=project_root,
        harness=normalized_harness,
        candidate_ids=[normalized_session_id],
        config_root_hint=config_root_hint,
    )
    if transcript_target is not None:
        output_target = _spawn_history_fallback_for_chat_ref(
            runtime_root=runtime_root,
            display_id=chat_id,
            chat_id=chat_id,
            primary_spawn=primary_spawn,
            related_spawns=related_spawns,
        )
        return _with_sources(transcript_target, output_target)

    detected_session_id = _detect_primary_session_id(
        project_root=project_root,
        runtime_root=runtime_root,
        spawn_row=primary_spawn,
        harness=normalized_harness,
    )
    if detected_session_id is not None and detected_session_id != normalized_session_id:
        transcript_target = _resolve_transcript_from_candidates(
            project_root=project_root,
            harness=normalized_harness,
            candidate_ids=[detected_session_id],
            config_root_hint=config_root_hint,
        )
        if transcript_target is not None:
            output_target = _spawn_history_fallback_for_chat_ref(
                runtime_root=runtime_root,
                display_id=chat_id,
                chat_id=chat_id,
                primary_spawn=primary_spawn,
                related_spawns=related_spawns,
            )
            return _with_sources(transcript_target, output_target)

    if primary_spawn is not None:
        output_target = _managed_primary_output_target(
            runtime_root,
            spawn_row=primary_spawn,
            display_id=chat_id,
            harness=normalized_harness,
        )
        if output_target is not None:
            return output_target

    return _resolve_harness_session_file(
        project_root=project_root,
        session_id=normalized_session_id,
        harness=normalized_harness,
        config_root_hint=config_root_hint,
    )


def _resolve_from_chat_id(
    *,
    project_root: Path,
    runtime_root: Path,
    chat_id: str,
) -> SessionLogTarget:
    session_record = _read_chat_session_record(runtime_root, chat_id)
    if session_record is None:
        raise ValueError(f"Chat '{chat_id}' not found")
    primary_spawn = _primary_spawn_for_chat(
        project_root,
        runtime_root,
        chat_id,
        session_record.spawn_id,
    )
    return _resolve_from_chat_state(
        project_root=project_root,
        runtime_root=runtime_root,
        chat_id=chat_id,
        session_record=session_record,
        primary_spawn=primary_spawn,
        related_spawns=() if session_record.spawn_id else None,
    )


def iter_chat_session_log_targets(
    *,
    project_root: Path,
    runtime_root: Path,
    chat_ids: Sequence[str],
) -> Iterator[ChatSessionLogTargetResolution]:
    """Resolve an ordered chat subset with bounded direct state reads."""

    normalized_chat_ids = tuple(chat_id.strip() for chat_id in chat_ids)
    records: dict[str, session_store.SessionRecord] = {
        str(record.chat_id): record
        for record in session_store.get_session_records(
            runtime_root,
            {chat_id for chat_id in normalized_chat_ids if chat_id},
        )
    }
    wanted_chat_ids = set(normalized_chat_ids)
    primary_spawns: dict[str, SpawnRecord] = {}
    related_spawns: dict[str, list[SpawnRecord]] = {
        chat_id: [] for chat_id in wanted_chat_ids
    }
    missing_relationships: set[str] = set()
    for chat_id, record in records.items():
        spawn_id = (record.spawn_id or "").strip()
        if not spawn_id:
            missing_relationships.add(chat_id)
            continue
        spawn = spawn_store.get_spawn(runtime_root, spawn_id)
        if (
            spawn is not None
            and spawn.kind == "primary"
            and session_identity.spawn_owner_chat_id(spawn) == chat_id
        ):
            primary_spawns[chat_id] = spawn

    if missing_relationships and not session_store.primary_spawn_backfill_is_current(
        runtime_root
    ):
        for spawn in spawn_store.list_spawns(runtime_root).records:
            raw_owner_chat_id = session_identity.spawn_owner_chat_id(spawn)
            owner_chat_id = (
                str(raw_owner_chat_id) if raw_owner_chat_id is not None else ""
            )
            exact_chat_id = str(spawn.chat_id) if spawn.chat_id is not None else ""
            if spawn.kind == "primary" and owner_chat_id in missing_relationships:
                primary_spawns[owner_chat_id] = spawn
            for related_chat_id in (
                {exact_chat_id, owner_chat_id} & missing_relationships
            ):
                related_spawns[related_chat_id].append(spawn)

    for chat_id in normalized_chat_ids:
        session_record = records.get(chat_id)
        if session_record is None:
            yield ChatSessionLogTargetResolution(
                chat_id,
                None,
                f"Chat '{chat_id}' not found",
            )
            continue
        try:
            target = _resolve_from_chat_state(
                project_root=project_root,
                runtime_root=runtime_root,
                chat_id=chat_id,
                session_record=session_record,
                primary_spawn=primary_spawns.get(chat_id),
                related_spawns=related_spawns.get(chat_id, ()),
            )
        except (ValueError, FileNotFoundError, OSError) as exc:
            yield ChatSessionLogTargetResolution(chat_id, None, str(exc))
            continue
        yield ChatSessionLogTargetResolution(chat_id, target)


def _spawn_linked_chat_session(
    *,
    runtime_root: Path,
    spawn_id: str,
    chat_id: str | None,
) -> session_store.SessionRecord | None:
    from meridian.lib.state.session_identity import get_session_record_for_spawn

    _ = chat_id
    return get_session_record_for_spawn(
        runtime_root,
        spawn_id,
        require_harness_session_id=True,
    )


def _resolve_from_spawn_id(
    *,
    project_root: Path,
    runtime_root: Path,
    spawn_id: str,
) -> SessionLogTarget:
    row = read_spawn_row_read_only(project_root, spawn_id, runtime_root=runtime_root)
    if row is None:
        raise ValueError(f"Spawn '{spawn_id}' not found")

    is_primary_spawn = row.kind == "primary"
    is_managed_backend_primary = is_primary_spawn and is_managed_primary(runtime_root, spawn_id)

    if output_target := _running_managed_primary_output_target(
        runtime_root,
        spawn_row=row,
        display_id=spawn_id,
    ):
        return output_target

    if (
        (not is_primary_spawn)
        and row.status in {"queued", "running"}
        and (
            output_target := _target_from_spawn_output(
                runtime_root,
                display_id=spawn_id,
                spawn_id=spawn_id,
            )
        )
    ):
        return output_target

    session_id = (row.harness_session_id or "").strip()
    harness = (row.harness or "").strip() or None
    config_root_hint = _config_root_hint(row.claude_config_dir)

    if not session_id and is_primary_spawn:
        primary_meta_session_id = read_primary_harness_session_id(runtime_root, spawn_id)
        if primary_meta_session_id is not None:
            session_id = primary_meta_session_id

    if not session_id:
        if is_primary_spawn:
            detected_session_id = _detect_primary_session_id(
                project_root=project_root,
                runtime_root=runtime_root,
                spawn_row=row,
                harness=harness,
            )
            if detected_session_id:
                session_id = detected_session_id
            elif is_managed_backend_primary and (
                output_target := _target_from_spawn_output(
                    runtime_root,
                    display_id=spawn_id,
                    spawn_id=spawn_id,
                    source=_managed_primary_fallback_source(spawn_id, harness),
                )
            ):
                return output_target
        else:
            output_target = _target_from_spawn_output(
                runtime_root,
                display_id=spawn_id,
                spawn_id=spawn_id,
            )
            if output_target is not None:
                return output_target
            record = _spawn_linked_chat_session(
                runtime_root=runtime_root,
                spawn_id=spawn_id,
                chat_id=row.chat_id,
            )
            if record is not None:
                session_id = (record.harness_session_id or "").strip()
                if record.harness.strip():
                    harness = record.harness.strip()
                if config_root_hint is None:
                    config_root_hint = _config_root_hint(record.claude_config_dir)
            if not session_id:
                raise ValueError(
                    f"Spawn '{spawn_id}' has no transcript available yet "
                    "(no harness session id recorded and no spawn output found)."
                )

    if not session_id:
        raise ValueError(
            f"Spawn '{spawn_id}' has no transcript available yet (no harness session id recorded)."
        )

    if harness is None:
        record = session_store.resolve_session_ref(runtime_root, session_id)
        if record is not None and record.harness.strip():
            harness = record.harness.strip()
        if record is not None and config_root_hint is None:
            config_root_hint = _config_root_hint(record.claude_config_dir)

    transcript_target = _resolve_transcript_from_candidates(
        project_root=project_root,
        harness=harness,
        candidate_ids=[session_id],
        config_root_hint=config_root_hint,
    )
    if transcript_target is not None:
        output_target = _resolved_spawn_output_fallback_target(
            runtime_root=runtime_root,
            row=row,
            display_id=spawn_id,
            is_primary_spawn=is_primary_spawn,
            is_managed_backend_primary=is_managed_backend_primary,
            harness=harness,
        )
        return _with_sources(transcript_target, output_target)

    if is_primary_spawn:
        detected_session_id = _detect_primary_session_id(
            project_root=project_root,
            runtime_root=runtime_root,
            spawn_row=row,
            harness=harness,
        )
        if detected_session_id is not None and detected_session_id != session_id:
            transcript_target = _resolve_transcript_from_candidates(
                project_root=project_root,
                harness=harness,
                candidate_ids=[detected_session_id],
                config_root_hint=config_root_hint,
            )
            if transcript_target is not None:
                output_target = _resolved_spawn_output_fallback_target(
                    runtime_root=runtime_root,
                    row=row,
                    display_id=spawn_id,
                    is_primary_spawn=is_primary_spawn,
                    is_managed_backend_primary=is_managed_backend_primary,
                    harness=harness,
                )
                return _with_sources(transcript_target, output_target)

    if is_primary_spawn:
        if is_managed_backend_primary:
            output_target = _managed_primary_output_target(
                runtime_root,
                display_id=spawn_id,
                spawn_row=row,
                harness=harness,
            )
            if output_target is not None:
                return output_target
        return _resolve_harness_session_file(
            project_root=project_root,
            session_id=session_id,
            harness=harness,
            config_root_hint=config_root_hint,
        )

    output_target = _target_from_spawn_output(
        runtime_root,
        display_id=spawn_id,
        spawn_id=spawn_id,
    )
    if output_target is not None:
        return output_target
    return _resolve_harness_session_file(
        project_root=project_root,
        session_id=session_id,
        harness=harness,
        config_root_hint=config_root_hint,
    )


def _resolve_from_session_ref(
    *,
    project_root: Path,
    runtime_root: Path,
    session_ref: str,
) -> SessionLogTarget:
    record = session_store.resolve_session_ref(runtime_root, session_ref)
    if record is not None:
        session_id = (record.harness_session_id or "").strip() or session_ref
        harness = record.harness.strip() or None
        target = _resolve_harness_session_file(
            project_root=project_root,
            session_id=session_id,
            harness=harness,
            config_root_hint=_config_root_hint(record.claude_config_dir),
        )
        output_target = _spawn_history_fallback_for_session_ref(
            project_root=project_root,
            runtime_root=runtime_root,
            display_id=session_id,
            record=record,
        )
        return _with_sources(target, output_target)

    target = _resolve_untracked_session_ref(project_root=project_root, session_ref=session_ref)
    output_target = _spawn_history_fallback_for_harness_session_id(
        runtime_root=runtime_root,
        display_id=session_ref,
        harness_session_id=session_ref,
    )
    return _with_sources(target, output_target)


def _resolve_untracked_session_ref(
    *, project_root: Path, session_ref: str
) -> SessionLogTarget:
    inferred = infer_harness_from_untracked_session_ref(project_root, session_ref)
    return _resolve_harness_session_file(
        project_root=project_root,
        session_id=session_ref,
        harness=str(inferred) if inferred is not None else None,
        config_root_hint=None,
    )


def resolve_session_log_target(
    *,
    ref: str,
    file_path: str | None,
    project_root: Path,
    runtime_root: Path | None,
) -> SessionLogTarget:
    if file_path is not None and file_path.strip():
        return _resolve_file_target(file_path)

    normalized_ref = ref.strip()
    if not normalized_ref:
        raise ValueError("Session reference is required unless --file is provided")

    if runtime_root is None:
        is_chat_id = normalized_ref.startswith("c") and normalized_ref[1:].isdigit()
        if _is_spawn_ref(normalized_ref) or is_chat_id:
            raise FileNotFoundError(f"Session reference '{normalized_ref}' not found")
        return _resolve_untracked_session_ref(
            project_root=project_root,
            session_ref=normalized_ref,
        )

    if _is_chat_ref(runtime_root, normalized_ref):
        return _resolve_from_chat_id(
            project_root=project_root,
            runtime_root=runtime_root,
            chat_id=normalized_ref,
        )

    if _is_spawn_ref(normalized_ref):
        return _resolve_from_spawn_id(
            project_root=project_root,
            runtime_root=runtime_root,
            spawn_id=normalized_ref,
        )

    return _resolve_from_session_ref(
        project_root=project_root,
        runtime_root=runtime_root,
        session_ref=normalized_ref,
    )


__all__ = [
    "ChatSessionLogTargetResolution",
    "SessionLogTarget",
    "iter_chat_session_log_targets",
    "resolve_session_log_target",
    "spawn_output_path_for_target",
]
