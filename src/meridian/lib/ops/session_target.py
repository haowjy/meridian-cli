"""Session-log target resolution helpers.

This module resolves user refs (chat, spawn, harness session id, or explicit file)
into a concrete transcript file target. It is intentionally read-only: no state
mutation or repair writes happen during resolution.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.harness.session_detection import infer_harness_from_untracked_session_ref
from meridian.lib.launch.constants import HISTORY_FILENAME, OUTPUT_FILENAME
from meridian.lib.ops.spawn.query import (
    read_latest_primary_spawn_for_chat_read_only,
    read_spawn_row_read_only,
)
from meridian.lib.state import session_store
from meridian.lib.state.paths import spawn_output_path
from meridian.lib.state.primary_meta import is_managed_primary, read_primary_harness_session_id
from meridian.lib.state.spawn.model import SpawnRecord

_CODEX_FILENAME_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(?P<session_id>[0-9a-fA-F-]{36})\.jsonl$"
)
_PRIMARY_TRANSCRIPT_UNAVAILABLE_SUFFIX = (
    "exists but no transcript is available yet (no harness session id recorded)."
)


class SessionLogTarget(NamedTuple):
    session_id: str
    harness: str | None
    file_path: Path
    source: str


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

    harness: str | None = None
    parts = set(resolved.parts)
    if ".claude" in parts:
        harness = "claude"
    elif ".codex" in parts:
        harness = "codex"

    return SessionLogTarget(
        session_id=_extract_session_id_from_path(resolved),
        harness=harness,
        file_path=resolved,
        source="file",
    )


def _resolve_harness_session_file(
    *,
    project_root: Path,
    session_id: str,
    harness: str | None,
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

        candidate = adapter.resolve_session_file(
            project_root=project_root,
            session_id=normalized_session_id,
        )
        if candidate is not None and candidate.is_file():
            return SessionLogTarget(
                session_id=normalized_session_id,
                harness=str(harness_id),
                file_path=candidate,
                source=f"{harness_id} transcript",
            )
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
        candidate = adapter.resolve_session_file(
            project_root=project_root,
            session_id=normalized_session_id,
        )
        if candidate is not None and candidate.is_file():
            return SessionLogTarget(
                session_id=normalized_session_id,
                harness=str(harness_id),
                file_path=candidate,
                source=f"{harness_id} transcript",
            )

    checked = ", ".join(checked_harnesses) if checked_harnesses else "<none>"
    raise FileNotFoundError(
        f"Session file for '{normalized_session_id}' not found. Checked harnesses: {checked}"
    )


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
    *,
    live_first: bool,
) -> Path | None:
    live_path = spawn_output_path(runtime_root, spawn_id)
    artifact_path = runtime_root / "artifacts" / spawn_id / HISTORY_FILENAME
    legacy_live_path = runtime_root / "spawns" / spawn_id / OUTPUT_FILENAME
    legacy_artifact_path = runtime_root / "artifacts" / spawn_id / OUTPUT_FILENAME

    history_candidates = (live_path, artifact_path) if live_first else (artifact_path, live_path)
    legacy_candidates = (
        (legacy_live_path, legacy_artifact_path)
        if live_first
        else (legacy_artifact_path, legacy_live_path)
    )
    candidates = history_candidates + legacy_candidates
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


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
    live_first: bool,
    source: str | None = None,
) -> SessionLogTarget | None:
    output_path = spawn_output_path_for_target(runtime_root, spawn_id, live_first=live_first)
    if output_path is None:
        return None
    return SessionLogTarget(
        session_id=display_id,
        harness=None,
        file_path=output_path,
        source=source or f"spawn {spawn_id} output",
    )


def _primary_spawn_for_chat(
    project_root: Path,
    runtime_root: Path,
    chat_id: str,
) -> SpawnRecord | None:
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
    normalized = record.harness_session_id.strip()
    return normalized or None


def _read_chat_session_record(
    runtime_root: Path, chat_id: str
) -> session_store.SessionRecord | None:
    records = session_store.get_session_records(runtime_root, {chat_id})
    if not records:
        return None
    return records[0]


def _resolve_from_chat_id(
    *,
    project_root: Path,
    runtime_root: Path,
    chat_id: str,
) -> SessionLogTarget:
    session_record = _read_chat_session_record(runtime_root, chat_id)
    if session_record is None:
        raise ValueError(f"Chat '{chat_id}' not found")
    primary_spawn = _primary_spawn_for_chat(project_root, runtime_root, chat_id)
    normalized_harness = session_record.harness.strip() or None
    if normalized_harness is None and primary_spawn is not None and primary_spawn.harness:
        normalized_harness = primary_spawn.harness.strip() or None

    normalized_session_id = _latest_harness_session_id(session_record)
    if (
        normalized_session_id is None
        and primary_spawn is not None
        and (
            primary_meta_session_id := read_primary_harness_session_id(
                runtime_root, primary_spawn.id
            )
        )
        is not None
    ):
        normalized_session_id = primary_meta_session_id.strip() or None

    if normalized_session_id is None:
        if primary_spawn is None:
            raise ValueError(_primary_transcript_unavailable_message(chat_id))
        normalized_session_id = _detect_primary_harness_session_id(
            project_root=project_root,
            spawn_row=primary_spawn,
            harness_hint=normalized_harness,
        )
        if normalized_session_id is None:
            raise ValueError(_primary_transcript_unavailable_message(chat_id))

    if not normalized_session_id.strip():
        raise ValueError(f"Chat '{chat_id}' not found")

    if normalized_harness is None:
        inferred = infer_harness_from_untracked_session_ref(project_root, normalized_session_id)
        normalized_harness = str(inferred) if inferred is not None else None

    try:
        return _resolve_harness_session_file(
            project_root=project_root,
            session_id=normalized_session_id,
            harness=normalized_harness,
        )
    except FileNotFoundError:
        if primary_spawn is None:
            raise
        detected_session_id = _detect_primary_harness_session_id(
            project_root=project_root,
            spawn_row=primary_spawn,
            harness_hint=normalized_harness,
        )
        if (
            detected_session_id is None
            or detected_session_id.strip() == normalized_session_id.strip()
        ):
            raise
        return _resolve_harness_session_file(
            project_root=project_root,
            session_id=detected_session_id,
            harness=normalized_harness,
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

    if is_managed_backend_primary and row.status in {"queued", "running"} and (
        output_target := _target_from_spawn_output(
            runtime_root,
            display_id=spawn_id,
            spawn_id=spawn_id,
            live_first=True,
        )
    ):
        return output_target

    if (not is_primary_spawn) and row.status in {"queued", "running"} and (
        output_target := _target_from_spawn_output(
            runtime_root,
            display_id=spawn_id,
            spawn_id=spawn_id,
            live_first=True,
        )
    ):
        return output_target

    session_id = (row.harness_session_id or "").strip()
    harness = (row.harness or "").strip() or None

    if not session_id and row.chat_id is not None:
        by_chat = session_store.get_session_harness_id(runtime_root, row.chat_id)
        session_id = (by_chat or "").strip()

    if not session_id and is_primary_spawn:
        primary_meta_session_id = read_primary_harness_session_id(runtime_root, spawn_id)
        if primary_meta_session_id is not None:
            session_id = primary_meta_session_id

    if not session_id:
        if is_primary_spawn:
            detected_session_id = _detect_primary_harness_session_id(
                project_root=project_root,
                spawn_row=row,
                harness_hint=harness,
            )
            if detected_session_id:
                session_id = detected_session_id
            elif is_managed_backend_primary and (
                output_target := _target_from_spawn_output(
                    runtime_root,
                    display_id=spawn_id,
                    spawn_id=spawn_id,
                    live_first=(row.status == "running"),
                    source=_managed_primary_fallback_source(spawn_id, harness),
                )
            ):
                return output_target
        else:
            output_target = _target_from_spawn_output(
                runtime_root,
                display_id=spawn_id,
                spawn_id=spawn_id,
                live_first=(row.status == "running"),
            )
            if output_target is not None:
                return output_target
            raise ValueError(
                f"Spawn '{spawn_id}' has no transcript available yet "
                "(no harness session id recorded and no spawn output found)."
            )

    if not session_id:
        raise ValueError(
            f"Spawn '{spawn_id}' has no transcript available yet "
            "(no harness session id recorded)."
        )

    if harness is None:
        record = session_store.resolve_session_ref(runtime_root, session_id)
        if record is not None and record.harness.strip():
            harness = record.harness.strip()

    try:
        return _resolve_harness_session_file(
            project_root=project_root,
            session_id=session_id,
            harness=harness,
        )
    except FileNotFoundError:
        if is_primary_spawn:
            detected_session_id = _detect_primary_harness_session_id(
                project_root=project_root,
                spawn_row=row,
                harness_hint=harness,
            )
            if (
                detected_session_id is not None
                and detected_session_id.strip()
                and detected_session_id.strip() != session_id.strip()
            ):
                return _resolve_harness_session_file(
                    project_root=project_root,
                    session_id=detected_session_id,
                    harness=harness,
                )
            if is_managed_backend_primary:
                output_target = _target_from_spawn_output(
                    runtime_root,
                    display_id=spawn_id,
                    spawn_id=spawn_id,
                    live_first=(row.status == "running"),
                    source=_managed_primary_fallback_source(spawn_id, harness),
                )
                if output_target is not None:
                    return output_target
            raise
        output_target = _target_from_spawn_output(
            runtime_root,
            display_id=spawn_id,
            spawn_id=spawn_id,
            live_first=False,
        )
        if output_target is not None:
            return output_target
        raise


def _resolve_from_session_ref(
    *,
    project_root: Path,
    runtime_root: Path,
    session_ref: str,
) -> SessionLogTarget:
    record = session_store.resolve_session_ref(runtime_root, session_ref)
    if record is not None:
        session_id = record.harness_session_id.strip() or session_ref
        harness = record.harness.strip() or None
        return _resolve_harness_session_file(
            project_root=project_root,
            session_id=session_id,
            harness=harness,
        )

    inferred = infer_harness_from_untracked_session_ref(project_root, session_ref)
    inferred_name = str(inferred) if inferred is not None else None
    return _resolve_harness_session_file(
        project_root=project_root,
        session_id=session_ref,
        harness=inferred_name,
    )


def resolve_session_log_target(
    *,
    ref: str,
    file_path: str | None,
    project_root: Path,
    runtime_root: Path,
) -> SessionLogTarget:
    if file_path is not None and file_path.strip():
        return _resolve_file_target(file_path)

    normalized_ref = ref.strip()
    if not normalized_ref:
        raise ValueError("Session reference is required unless --file is provided")

    if normalized_ref.startswith("c") and normalized_ref[1:].isdigit():
        return _resolve_from_chat_id(
            project_root=project_root,
            runtime_root=runtime_root,
            chat_id=normalized_ref,
        )

    if normalized_ref.startswith("p") and normalized_ref[1:].isdigit():
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
