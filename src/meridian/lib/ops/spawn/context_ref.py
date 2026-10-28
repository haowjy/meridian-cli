"""Resolve and render prior context references for spawn prompts."""

import re
from pathlib import Path

from meridian.lib.core.context_ref import (
    ContextRef,
    SessionContextRef,
    SpawnContextRef,
)
from meridian.lib.core.refs import RefNotFound
from meridian.lib.ops.locate import Untracked, chat_spawn, locate
from meridian.lib.ops.work_attachment import resolve_active_work_id
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.spawn.model import SpawnRecord

from .query import read_report, read_spawn_row

_WINDOWS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _is_filesystem_path_ref(value: str) -> bool:
    """Return True when a ``--from`` value looks like a filesystem path, not a session ref."""

    normalized = value.strip()
    if not normalized or normalized.startswith("@"):
        return False
    if normalized.startswith(("./", "../", "/", "~")):
        return True
    if "\\" in normalized:
        return True
    if "/" in normalized:
        return True
    return bool(_WINDOWS_DRIVE_PATH_RE.match(normalized))


def _reject_filesystem_path_context_ref(value: str) -> None:
    if not _is_filesystem_path_ref(value):
        return
    raise ValueError(
        f"--from does not accept filesystem paths ({value!r}). "
        "Use a spawn id (p123), chat id (c123), or harness session id. "
        "To include file contents in the prompt, use --file/-f instead."
    )


def _load_report_text(project_root: Path, spawn_id: str) -> str | None:
    _, report_text = read_report(project_root, spawn_id)
    return report_text


def resolve_context_ref(project_root: Path, ref: str) -> ContextRef:
    """Resolve one --from value to concrete prior context payload.

    A spawn ref (or selector) names that spawn's report; a chat ref or native ID
    names that chat's own session.
    """

    normalized = ref.strip()
    if not normalized:
        raise ValueError("context reference is required")

    _reject_filesystem_path_context_ref(normalized)

    located = locate(project_root, normalized)
    if isinstance(located, Untracked):
        raise RefNotFound(located.native_id, "native")
    root = located.runtime_root
    if located.spawn is not None:
        row = read_spawn_row(project_root, located.spawn.id, runtime_root=root)
        if row is None:
            raise RefNotFound(located.spawn.id, "spawn")
        return _spawn_context_ref(row, project_root)

    chat = located.chat
    assert chat is not None
    owner = chat_spawn(located)
    owner_row = spawn_store.get_spawn(root, owner) if owner is not None else None
    if owner_row is not None:
        return _session_context_ref(owner_row, chat, project_root, root)
    if not (chat.harness_session_id or "").strip():
        raise ValueError(
            f"Session '{chat.chat_id}' exists but no transcript is available yet "
            "(no harness session id recorded)."
        )
    return _session_context_ref_from_record(chat)


def _spawn_context_ref(row: SpawnRecord, project_root: Path) -> SpawnContextRef:
    return SpawnContextRef(
        spawn_id=row.id,
        status=row.status,
        agent=row.agent or "",
        desc=row.desc or "",
        model=row.model or "",
        harness=row.harness or "",
        report_text=_load_report_text(project_root, row.id),
        harness_session_id=row.harness_session_id,
        chat_id=row.continue_chat_id,
        history_id=row.history_id,
        work_id=(row.work_id or "").strip() or None,
        task_cwd=(row.task_cwd or "").strip() or None,
    )


def _session_context_ref(
    primary_row: SpawnRecord,
    record: session_store.SessionRecord,
    project_root: Path,
    runtime_root: Path,
) -> SessionContextRef:
    """Name the session ``record`` as its owner ran it: a chat ID names its own session."""

    return SessionContextRef(
        chat_id=record.chat_id,
        primary_spawn_id=primary_row.id,
        history_id=record.history_id or primary_row.history_id,
        status=primary_row.status,
        agent=primary_row.agent or "",
        model=primary_row.model or "",
        harness=primary_row.harness or "",
        harness_session_id=(record.harness_session_id or "").strip() or None,
        work_id=resolve_active_work_id(project_root, runtime_root, chat_id=record.chat_id),
        task_cwd=(primary_row.task_cwd or "").strip() or None,
    )


def _session_context_ref_from_record(
    session: session_store.SessionRecord,
) -> SessionContextRef:
    """Build transcript context when a primary session has no spawn row."""

    return SessionContextRef(
        chat_id=session.chat_id,
        history_id=session.history_id,
        status="stopped" if session.stopped_at is not None else "running",
        agent=session.agent,
        model=session.model,
        harness=session.harness,
        harness_session_id=(session.harness_session_id or "").strip() or None,
        work_id=(session.active_work_id or "").strip() or None,
        task_cwd=(session.task_cwd or session.execution_cwd or "").strip() or None,
    )


__all__ = ["resolve_context_ref"]
