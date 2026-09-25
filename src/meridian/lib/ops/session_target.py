"""Session-log target resolution helpers.

This module resolves user refs (chat, spawn, harness session id, or explicit file)
into a concrete transcript file target. Display prefers the history index, then
live/untracked native files. It is intentionally read-only: no state mutation
or repair writes happen during resolution.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, NamedTuple

from meridian.lib.core.domain import TERMINAL_SPAWN_STATUSES
from meridian.lib.core.native_identity import NativeSessionUnavailable
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.adapter import SubprocessHarness
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.harness.session_detection import infer_harness_from_untracked_session_ref
from meridian.lib.ops.spawn.query import read_spawn_row_read_only
from meridian.lib.state import session_identity, session_store
from meridian.lib.state.history_index import HistoryIndex
from meridian.lib.state.paths import resolve_spawn_output_path

_CODEX_FILENAME_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(?P<session_id>[0-9a-fA-F-]{36})\.jsonl$"
)
class TranscriptSource(NamedTuple):
    kind: Literal["file", "native_file", "opencode_db", "spawn_history", "archive"]
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
    native_store: Path | None = None,
    tracked: bool = True,
) -> SessionLogTarget | None:
    if native_store is not None:
        candidate = adapter.resolve_native_session_file(
            project_root=project_root, session_id=session_id, native_store=native_store,
        )
    elif not tracked:
        candidate = adapter.resolve_session_file(
            project_root=project_root, session_id=session_id, config_root_hint=config_root_hint,
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
            config_root_hint=config_root_hint, native_store=native_store, tracked=tracked,
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
            config_root_hint=config_root_hint, native_store=native_store, tracked=tracked,
        )
    except FileNotFoundError:
        return None


def spawn_output_path_for_target(
    runtime_root: Path,
    spawn_id: str,
) -> Path | None:
    return resolve_spawn_output_path(runtime_root, spawn_id)


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


def _config_root_hint(value: str | None) -> Path | None:
    normalized = (value or "").strip()
    return Path(normalized).expanduser() if normalized else None


def _read_chat_session_record(
    runtime_root: Path, chat_id: str
) -> session_store.SessionRecord | None:
    return session_store.get_session_record(runtime_root, chat_id)


def _resolve_from_chat_id(
    *,
    project_root: Path,
    runtime_root: Path,
    chat_id: str,
) -> SessionLogTarget:
    session_record = _read_chat_session_record(runtime_root, chat_id)
    if session_record is None:
        raise ValueError(f"Chat '{chat_id}' not found")
    session_id = session_record.harness_session_id
    native_store = session_record.native_store
    if not session_id or not session_record.harness or not native_store:
        raise NativeSessionUnavailable(chat_id, "unbound")
    target = _resolve_harness_transcript_target_or_none(
        project_root=Path(session_record.execution_cwd or session_record.task_cwd or project_root),
        session_id=session_id,
        harness=session_record.harness,
        native_store=_config_root_hint(native_store),
        config_root_hint=_config_root_hint(session_record.claude_config_dir),
    )
    if target is None:
        raise NativeSessionUnavailable(chat_id, "missing")
    return target


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
        require_harness_session_id=False,
    )


def _resolve_from_spawn_id(
    *,
    project_root: Path,
    runtime_root: Path,
    spawn_id: str,
    purpose: Literal["display", "capture"] = "display",
) -> SessionLogTarget:
    row = read_spawn_row_read_only(project_root, spawn_id, runtime_root=runtime_root)
    if row is None:
        raise ValueError(f"Spawn '{spawn_id}' not found")

    if purpose == "capture":
        if (
            row.record_mode == "historical"
            or row.status not in TERMINAL_SPAWN_STATUSES
            or row.history_id is None
        ):
            raise ValueError("Capture preparation requires an identified terminal record")
        if row.kind != "primary":
            stream = _target_from_spawn_output(runtime_root, display_id=spawn_id, spawn_id=spawn_id)
            if stream is None:
                raise FileNotFoundError(f"No retained child stream available for {spawn_id}")
            return stream
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
                session.claude_config_dir
                if session
                else row.claude_config_dir
            ),
        )
        # The provider chooses one exact native source, including positive-empty DB
        # sessions. Capture must not follow presentation's output/legacy fallbacks.
        return _target_from_source(target.sources[0])

    if row.run_boundary is not None:
        chat_id = row.continue_chat_id
        if chat_id:
            target = _resolve_from_chat_id(
                project_root=project_root, runtime_root=runtime_root, chat_id=chat_id,
            )
            if row.run_boundary.status != "verified":
                label = target.source + " (entry-based view)"
                return target._replace(
                    source=label, view_label="entry-based view (exit identity unresolved)",
                    sources=tuple(source._replace(source_label=label) for source in target.sources),
                )
            return target

    record = _spawn_linked_chat_session(
        runtime_root=runtime_root, spawn_id=spawn_id, chat_id=row.chat_id,
    )
    session_id = record.harness_session_id if record is not None else row.harness_session_id
    harness = record.harness if record is not None else row.harness
    if not session_id or not harness or record is None or not record.native_store:
        raise NativeSessionUnavailable(spawn_id, "unbound")
    target = _resolve_harness_transcript_target_or_none(
        project_root=Path(row.execution_cwd or row.task_cwd or project_root),
        session_id=session_id, harness=harness,
        native_store=_config_root_hint(record.native_store if record else None),
        config_root_hint=_config_root_hint(
            record.claude_config_dir if record else row.claude_config_dir
        ),
    )
    if target is None:
        raise NativeSessionUnavailable(spawn_id, "missing")
    return target


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
        return _resolve_harness_session_file(
            project_root=project_root,
            session_id=session_id,
            harness=harness,
            native_store=_config_root_hint(record.native_store),
            config_root_hint=_config_root_hint(record.claude_config_dir),
        )

    return _resolve_untracked_session_ref(project_root=project_root, session_ref=session_ref)


def _resolve_untracked_session_ref(*, project_root: Path, session_ref: str) -> SessionLogTarget:
    inferred = infer_harness_from_untracked_session_ref(project_root, session_ref)
    return _resolve_harness_session_file(
        project_root=project_root,
        session_id=session_ref,
        harness=str(inferred) if inferred is not None else None,
        config_root_hint=None, tracked=False,
    )


def indexed_history_target(
    runtime_root: Path, ref: str, project_root: Path, *, deadline: float | None = None
) -> SessionLogTarget | None:
    from meridian.lib.config.settings import load_config

    configured = load_config(project_root).history.archive.destination
    targets = HistoryIndex(runtime_root).read_targets(
        ref, destination=Path(configured).expanduser() if configured else None, deadline=deadline
    )
    if not targets:
        return None
    sources = tuple(
        TranscriptSource(
            kind="archive" if target.archive_id else "spawn_history",
            session_id=ref,
            harness=target.state.harness,
            source_label="Archived Meridian history"
            if target.archive_id
            else f"spawn {target.state.id} output",
            path=target.path,
            history_id=str(target.state.history_id) if target.state.history_id else None,
            manifest_sha256=target.manifest_sha256,
        )
        for target in targets
    )
    return _target_from_source(sources[0])._replace(sources=sources)


def resolve_session_log_target(
    *,
    ref: str,
    file_path: str | None,
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
            project_root=project_root, runtime_root=runtime_root, chat_id=normalized_ref,
        )

    if runtime_root is not None and _is_spawn_ref(normalized_ref):
        from meridian.lib.state.spawn_store import get_spawn

        row = get_spawn(runtime_root, normalized_ref)
        if row is not None and row.run_boundary is not None:
            return _resolve_from_spawn_id(
                project_root=project_root, runtime_root=runtime_root, spawn_id=normalized_ref,
            )

    if runtime_root is not None:
        indexed = indexed_history_target(
            runtime_root, normalized_ref, project_root, deadline=deadline
        )
        if indexed is not None:
            return indexed

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
        )

    return _resolve_from_session_ref(
        project_root=project_root,
        runtime_root=runtime_root,
        session_ref=normalized_ref,
    )


__all__ = [
    "SessionLogTarget",
    "resolve_session_log_target",
    "spawn_output_path_for_target",
]
