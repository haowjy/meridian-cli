"""Session-log target resolution helpers.

This module resolves user refs (chat, spawn, harness session id, or explicit file)
into one exact native transcript. The metadata index only supplies reclaimed
spawn records and aliases; session bindings remain file-authoritative. Resolution
does not repair or mutate authoritative state.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, NamedTuple

from sqlalchemy import or_, select

from meridian.lib.core.domain import TERMINAL_SPAWN_STATUSES
from meridian.lib.core.native_identity import NativeSessionUnavailable
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.adapter import SubprocessHarness
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.harness.session_detection import infer_harness_from_untracked_session_ref
from meridian.lib.harness.transcript import reject_runner_history
from meridian.lib.ops.run_boundary import spawn_view_label
from meridian.lib.ops.spawn.query import read_spawn_row_read_only
from meridian.lib.state import session_identity, session_store
from meridian.lib.state.history_index import ALIASES, RECORDS, HistoryIndex
from meridian.lib.state.spawn.model import SpawnRecord

_CODEX_FILENAME_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(?P<session_id>[0-9a-fA-F-]{36})\.jsonl$"
)


class TranscriptSource(NamedTuple):
    kind: Literal["file", "native_file", "opencode_db", "archive"]
    session_id: str
    harness: str | None
    source_label: str
    path: Path | None = None
    history_id: str | None = None
    manifest_sha256: str | None = None


class SessionLogTarget(NamedTuple):
    session_id: str
    harness: str | None
    file_path: Path | None
    source: str
    sources: tuple[TranscriptSource, ...]
    view_label: str | None = None


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

    reject_runner_history(resolved)
    return _target_from_source(
        TranscriptSource(
            kind="file",
            session_id=_extract_session_id_from_path(resolved),
            harness=harness,
            path=resolved,
            source_label="file",
        )
    )._replace(view_label="file")


def _resolve_adapter_file_target(
    *,
    project_root: Path,
    session_id: str,
    harness_id: HarnessId,
    adapter: SubprocessHarness,
    config_root_hint: Path | None,
    native_store: Path | None = None,
    tracked: bool = True,
) -> SessionLogTarget | None:
    if native_store is not None:
        candidate = adapter.resolve_native_session_file(
            session_id=session_id,
            native_store=native_store,
        )
    elif not tracked:
        candidate = adapter.resolve_session_file(
            project_root=project_root,
            session_id=session_id,
            config_root_hint=config_root_hint,
        )
    else:
        raise NativeSessionUnavailable(session_id, "unbound")
    if candidate is None or not candidate.is_file():
        return None
    return _target_from_source(
        TranscriptSource(
            kind=adapter.native_transcript_kind(candidate),
            session_id=session_id,
            harness=str(harness_id),
            path=candidate,
            source_label=f"{harness_id} transcript",
        )
    )


def _resolve_harness_session_file(
    *,
    project_root: Path,
    session_id: str,
    harness: str | None,
    config_root_hint: Path | None,
    native_store: Path | None = None,
    tracked: bool = True,
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
            native_store=native_store,
            tracked=tracked,
        )
        if file_target is not None:
            return file_target
        raise FileNotFoundError(
            f"Session file for '{normalized_session_id}' (harness={normalized_harness}) not found"
        )

    raise NativeSessionUnavailable(normalized_session_id, "unbound")


def _resolve_harness_transcript_target_or_none(
    *,
    project_root: Path,
    session_id: str,
    harness: str | None,
    config_root_hint: Path | None,
    native_store: Path | None = None,
    tracked: bool = True,
) -> SessionLogTarget | None:
    try:
        return _resolve_harness_session_file(
            project_root=project_root,
            session_id=session_id,
            harness=harness,
            config_root_hint=config_root_hint,
            native_store=native_store,
            tracked=tracked,
        )
    except FileNotFoundError:
        return None


def _config_root_hint(value: str | None) -> Path | None:
    normalized = (value or "").strip()
    return Path(normalized).expanduser() if normalized else None


def _resolve_from_chat_id(
    *,
    project_root: Path,
    runtime_root: Path,
    chat_id: str,
) -> SessionLogTarget:
    session_record = session_store.get_session_record(runtime_root, chat_id)
    if session_record is None:
        raise ValueError(f"Chat '{chat_id}' not found")
    return _target_from_record(project_root, session_record)


def _target_from_record(
    project_root: Path, session_record: session_store.SessionRecord
) -> SessionLogTarget:
    chat_id = session_record.chat_id
    key = session_record.native_key()
    if key is None:
        raise NativeSessionUnavailable(chat_id, "unbound")
    target = _resolve_harness_transcript_target_or_none(
        project_root=Path(session_record.execution_cwd or session_record.task_cwd or project_root),
        session_id=key.session_id,
        harness=key.harness,
        native_store=Path(key.native_store),
        config_root_hint=_config_root_hint(session_record.claude_config_dir),
    )
    if target is None:
        raise NativeSessionUnavailable(chat_id, "missing")
    return target


def _indexed_spawn(
    runtime_root: Path, ref: str, *, deadline: float | None = None
) -> SpawnRecord | None:
    """Recover only the record, never a transcript location, from the projection."""
    with HistoryIndex(runtime_root).query(deadline=deadline) as db:
        records = (
            db.execute(
                select(RECORDS.c.record_json)
                .where(
                    or_(
                        RECORDS.c.history_id == ref,
                        RECORDS.c.history_id.in_(
                            select(ALIASES.c.history_id).where(
                                ALIASES.c.alias == ref,
                                ALIASES.c.kind != "harness",
                            )
                        ),
                    )
                )
                .distinct()
            )
            .scalars()
            .all()
        )
    if len(records) > 1:
        raise ValueError("Ambiguous archive origin alias; use a portable history UUID")
    return SpawnRecord.model_validate_json(records[0]) if records else None


def _spawn_target(*, row: SpawnRecord, project_root: Path, runtime_root: Path) -> SessionLogTarget:
    if row.chat_id is None:
        raise NativeSessionUnavailable(row.id, "unbound")
    chat_id = row.continue_chat_id
    record = session_store.get_session_record(runtime_root, chat_id) if chat_id else None
    if record is None:
        raise NativeSessionUnavailable(row.id, "unbound")
    target = _target_from_record(project_root, record)
    return target._replace(view_label=spawn_view_label(row))


def _resolve_from_spawn_id(
    *,
    project_root: Path,
    runtime_root: Path,
    spawn_id: str,
    purpose: Literal["display", "capture"] = "display",
    deadline: float | None = None,
) -> SessionLogTarget:
    row = read_spawn_row_read_only(project_root, spawn_id, runtime_root=runtime_root)
    if row is None and purpose == "display":
        row = _indexed_spawn(runtime_root, spawn_id, deadline=deadline)
    if row is None:
        raise ValueError(f"Spawn '{spawn_id}' not found")

    if purpose == "capture":
        if (
            row.record_mode == "historical"
            or row.status not in TERMINAL_SPAWN_STATUSES
            or row.history_id is None
        ):
            raise ValueError("Capture preparation requires an identified terminal record")
        # Use only this aggregate and its exact session generation. A current chat,
        # inferred harness or post-launch file discovery cannot establish binding.
        session = session_identity.session_records_for_spawns(runtime_root, [row]).get(row.id)
        harnesses, native_ids = session_identity.native_identity_candidates(
            runtime_root, row, session
        )
        if len(native_ids) > 1 or len(harnesses) > 1:
            raise ValueError(f"Conflicting native identity for capture: {row.id}")
        if not native_ids or not harnesses:
            raise ValueError(f"Native capture requires exact native identity: {row.id}")
        target = _resolve_harness_session_file(
            project_root=project_root,
            session_id=next(iter(native_ids)),
            harness=next(iter(harnesses)),
            native_store=_config_root_hint(session.native_store if session else None),
            config_root_hint=_config_root_hint(
                session.claude_config_dir if session else row.claude_config_dir
            ),
        )
        # The provider chooses one exact native source, including positive-empty DB
        # sessions. Capture must not follow presentation's output/legacy fallbacks.
        return _target_from_source(target.sources[0])

    return _spawn_target(row=row, project_root=project_root, runtime_root=runtime_root)


def _resolve_from_session_ref(
    *,
    project_root: Path,
    runtime_root: Path,
    session_ref: str,
    deadline: float | None = None,
) -> SessionLogTarget:
    matches = [
        record
        for record in session_store.list_all_session_records(runtime_root)
        if (key := record.native_key()) is not None and key.session_id == session_ref
    ]
    if matches:
        if len({record.native_key() for record in matches}) > 1:
            raise NativeSessionUnavailable(session_ref, "ambiguous_native_file")
        matches.sort(
            key=lambda record: (
                int(record.chat_id[1:]) if record.chat_id[1:].isdigit() else float("inf"),
                record.chat_id,
            )
        )
        target = _target_from_record(project_root, matches[0])
        return target._replace(
            view_label=(
                "also bound to " + ", ".join(record.chat_id for record in matches[1:])
                if len(matches) > 1
                else None
            )
        )
    row = _indexed_spawn(runtime_root, session_ref, deadline=deadline)
    if row is not None:
        return _spawn_target(row=row, project_root=project_root, runtime_root=runtime_root)
    return _resolve_untracked_session_ref(project_root=project_root, session_ref=session_ref)


def _resolve_untracked_session_ref(*, project_root: Path, session_ref: str) -> SessionLogTarget:
    inferred = infer_harness_from_untracked_session_ref(project_root, session_ref)
    return _resolve_harness_session_file(
        project_root=project_root,
        session_id=session_ref,
        harness=str(inferred) if inferred is not None else None,
        config_root_hint=None,
        tracked=False,
    )._replace(view_label="untracked")


def resolve_transcript_source(
    ref: str,
    *,
    file_path: str | None = None,
    project_root: Path,
    runtime_root: Path | None,
    deadline: float | None = None,
    purpose: Literal["display", "capture"] = "display",
) -> SessionLogTarget:
    if purpose == "capture":
        if runtime_root is None or file_path or not _is_spawn_ref(ref.strip()):
            raise ValueError("Native capture requires an exact local spawn reference")
        return _resolve_from_spawn_id(
            project_root=project_root,
            runtime_root=runtime_root,
            spawn_id=ref.strip(),
            purpose=purpose,
        )
    if file_path is not None and file_path.strip():
        return _resolve_file_target(file_path)

    normalized_ref = ref.strip()
    if not normalized_ref:
        raise ValueError("Session reference is required unless --file is provided")

    if runtime_root is not None and _is_chat_ref(runtime_root, normalized_ref):
        return _resolve_from_chat_id(
            project_root=project_root,
            runtime_root=runtime_root,
            chat_id=normalized_ref,
        )

    if runtime_root is None:
        is_chat_id = normalized_ref.startswith("c") and normalized_ref[1:].isdigit()
        if _is_spawn_ref(normalized_ref) or is_chat_id:
            raise FileNotFoundError(f"Session reference '{normalized_ref}' not found")
        return _resolve_untracked_session_ref(
            project_root=project_root,
            session_ref=normalized_ref,
        )

    if _is_spawn_ref(normalized_ref):
        return _resolve_from_spawn_id(
            project_root=project_root,
            runtime_root=runtime_root,
            spawn_id=normalized_ref,
            deadline=deadline,
        )

    return _resolve_from_session_ref(
        project_root=project_root,
        runtime_root=runtime_root,
        session_ref=normalized_ref,
        deadline=deadline,
    )


# Capture callers retain this entry point until A1 removes the capture branch.
resolve_session_log_target = resolve_transcript_source


__all__ = [
    "SessionLogTarget",
    "resolve_session_log_target",
    "resolve_transcript_source",
]
