"""Session-log target resolution helpers.

This module resolves user refs (chat, spawn, harness session id, or explicit file)
into one exact native transcript. The metadata index only supplies reclaimed
spawn records and aliases; session bindings remain file-authoritative. Resolution
does not repair or mutate authoritative state.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal, NamedTuple

from sqlalchemy import or_, select

from meridian.lib.core.domain import TERMINAL_SPAWN_STATUSES
from meridian.lib.core.native_identity import NativeKey, NativeSessionUnavailable
from meridian.lib.core.types import HarnessId
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


def native_source_label(harness: str) -> str:
    """One label for a native source across log, preview and search."""
    return f"{harness} transcript"


class TranscriptSource(NamedTuple):
    kind: Literal["file", "native_file", "opencode_db"]
    session_id: str
    harness: str | None
    source_label: str
    path: Path

    @classmethod
    def native(cls, harness: str, session_id: str, path: Path) -> TranscriptSource:
        """The one native source rule: the adapter names the kind of its own file."""
        adapter = get_default_harness_registry().get_subprocess_harness(HarnessId(harness))
        return cls(
            kind=adapter.native_transcript_kind(path),
            session_id=session_id,
            harness=harness,
            source_label=native_source_label(harness),
            path=path,
        )


class SessionLogTarget(NamedTuple):
    source: TranscriptSource
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


def _resolve_file_target(file_path: str) -> SessionLogTarget:
    resolved = Path(file_path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Session file '{resolved.as_posix()}' not found")

    reject_runner_history(resolved)
    with resolved.open("rb") as handle:
        prefix = handle.read(16)
    if prefix == b"SQLite format 3\x00":
        raise ValueError(
            "OpenCode native history is stored in its database; use `meridian session log cN`."
        )
    if resolved.suffix.lower() != ".jsonl":
        raise ValueError("not a native transcript")

    with resolved.open("rb") as handle:
        first = handle.readline(64 * 1024 + 1)
    try:
        header = json.loads(first)
    except (UnicodeDecodeError, ValueError):
        header = None
    is_claude = (
        isinstance(header, dict)
        and isinstance(header.get("sessionId"), str)
        and isinstance(header.get("type"), str)
    )
    is_codex = isinstance(header, dict) and header.get("type") in {
        "session_meta",
        "event_msg",
        "response_item",
    }
    is_pi = isinstance(header, dict) and header.get("type") == "session" and bool(header.get("id"))
    if not (is_claude or is_codex or is_pi):
        raise ValueError("not a native transcript")

    harness: str | None = None
    parts = set(resolved.parts)
    if ".claude" in parts:
        harness = "claude"
    elif ".codex" in parts:
        harness = "codex"

    return SessionLogTarget(
        TranscriptSource(
            kind="file",
            session_id=_extract_session_id_from_path(resolved),
            harness=harness,
            path=resolved,
            source_label="file",
        ),
        view_label="file",
    )


def _native_target(key: NativeKey, record: session_store.SessionRecord) -> SessionLogTarget:
    adapter = get_default_harness_registry().get_subprocess_harness(HarnessId(key.harness))
    candidate = adapter.resolve_native_session_file(
        session_id=key.session_id, native_store=Path(key.native_store)
    )
    if candidate is None or not candidate.is_file():
        raise NativeSessionUnavailable(record.chat_id, "missing")
    return SessionLogTarget(TranscriptSource.native(key.harness, key.session_id, candidate))


def _target_from_record(record: session_store.SessionRecord) -> SessionLogTarget:
    key = record.native_key()
    if key is None:
        raise NativeSessionUnavailable(record.chat_id, "unbound")
    return _native_target(key, record)


def _resolve_from_chat_id(*, runtime_root: Path, chat_id: str) -> SessionLogTarget:
    record = session_store.get_session_record(runtime_root, chat_id)
    if record is None:
        raise ValueError(f"Chat '{chat_id}' not found")
    return _target_from_record(record)


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


def _spawn_target(
    *, row: SpawnRecord, record_for: Callable[[str], session_store.SessionRecord | None]
) -> SessionLogTarget:
    if row.chat_id is None:
        raise NativeSessionUnavailable(row.id, "unbound")
    chat_id = row.continue_chat_id
    record = record_for(chat_id) if chat_id else None
    if record is None:
        raise NativeSessionUnavailable(row.id, "unbound")
    target = _target_from_record(record)
    return target._replace(view_label=spawn_view_label(row))


def _stored_record(runtime_root: Path) -> Callable[[str], session_store.SessionRecord | None]:
    return lambda chat_id: session_store.get_session_record(runtime_root, chat_id)


def resolve_run_sources(
    row: SpawnRecord, sessions: Mapping[str, session_store.SessionRecord]
) -> tuple[TranscriptSource, ...]:
    """Every exact native source holding a run's turns: its log chat and its entry chat."""
    sources = [_spawn_target(row=row, record_for=sessions.get).source]
    if row.chat_id is not None and row.chat_id != row.continue_chat_id:
        entry = sessions.get(row.chat_id)
        if entry is None:
            raise NativeSessionUnavailable(row.id, "unbound")
        sources.append(_target_from_record(entry).source)
    return tuple(sources)


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
        key = session.native_key() if session else None
        if key is None or session is None:
            raise NativeSessionUnavailable(row.id, "unbound")
        return _native_target(key, session)

    return _spawn_target(row=row, record_for=_stored_record(runtime_root))


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
        target = _target_from_record(matches[0])
        return target._replace(
            view_label=(
                "also bound to " + ", ".join(record.chat_id for record in matches[1:])
                if len(matches) > 1
                else None
            )
        )
    row = _indexed_spawn(runtime_root, session_ref, deadline=deadline)
    if row is not None:
        return _spawn_target(row=row, record_for=_stored_record(runtime_root))
    return _untracked_target(project_root=project_root, session_ref=session_ref)


def _untracked_target(*, project_root: Path, session_ref: str) -> SessionLogTarget:
    inferred = infer_harness_from_untracked_session_ref(project_root, session_ref)
    if inferred is None:
        raise NativeSessionUnavailable(session_ref, "unbound")
    adapter = get_default_harness_registry().get_subprocess_harness(inferred)
    candidate = adapter.resolve_session_file(
        project_root=project_root, session_id=session_ref, config_root_hint=None
    )
    if candidate is None or not candidate.is_file():
        raise FileNotFoundError(f"Session file for '{session_ref}' (harness={inferred}) not found")
    return SessionLogTarget(
        TranscriptSource.native(str(inferred), session_ref, candidate), view_label="untracked"
    )


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
            runtime_root=runtime_root,
            chat_id=normalized_ref,
        )

    if runtime_root is None:
        is_chat_id = normalized_ref.startswith("c") and normalized_ref[1:].isdigit()
        if _is_spawn_ref(normalized_ref) or is_chat_id:
            raise FileNotFoundError(f"Session reference '{normalized_ref}' not found")
        return _untracked_target(
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


__all__ = [
    "SessionLogTarget",
    "TranscriptSource",
    "native_source_label",
    "resolve_run_sources",
    "resolve_transcript_source",
]
