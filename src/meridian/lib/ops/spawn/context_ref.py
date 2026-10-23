"""Resolve and render prior context references for spawn prompts."""

import re
from pathlib import Path

from meridian.lib.core.context_ref import (
    CHAT_REF_RE,
    ContextRef,
    SessionContextRef,
    SpawnContextRef,
)
from meridian.lib.ops.runtime import resolve_runtime_root_for_read
from meridian.lib.state import session_identity, session_store
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_status import overlay_derived

from .query import (
    read_report,
    read_spawn_row,
    resolve_spawn_reference,
)

_SPAWN_REF_RE = re.compile(r"^p\d+$")
_WINDOWS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _is_filesystem_path_ref(value: str) -> bool:
    """Return True when a ``--from`` value looks like a filesystem path, not a session ref."""

    normalized = value.strip()
    if not normalized or normalized.startswith("@"):
        return False
    if _SPAWN_REF_RE.fullmatch(normalized) or CHAT_REF_RE.fullmatch(normalized):
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


def _select_primary_spawn_for_session(project_root: Path, chat_id: str) -> SpawnRecord | None:
    runtime_root = resolve_runtime_root_for_read(project_root)
    if runtime_root is None:
        return None
    owner_chat_id = session_identity.get_owner_chat_for_session(runtime_root, chat_id) or chat_id
    spawns = overlay_derived(
        runtime_root,
        session_identity.list_spawns_for_owner_chat(runtime_root, owner_chat_id).records,
    )
    primary_spawns = [row for row in spawns if row.kind == "primary"]
    if not primary_spawns:
        return None

    return primary_spawns[-1]


def _is_tracked_session(project_root: Path, chat_id: str) -> bool:
    runtime_root = resolve_runtime_root_for_read(project_root)
    if runtime_root is None:
        return False
    return bool(session_store.get_session_records(runtime_root, {chat_id}))


def _load_report_text(project_root: Path, spawn_id: str) -> str | None:
    _, report_text = read_report(project_root, spawn_id)
    return report_text


def _session_context_from(
    project_root: Path,
    chat_id: str,
    session: session_store.SessionRecord | None,
) -> SessionContextRef | None:
    primary_row = _select_primary_spawn_for_session(project_root, chat_id)
    if primary_row is not None:
        return _session_context_ref(primary_row, project_root)
    if session is not None:
        return _session_context_ref_from_record(session)
    return None


def resolve_context_ref(project_root: Path, ref: str) -> ContextRef:
    """Resolve one --from value to concrete prior context payload."""

    normalized = ref.strip()
    if not normalized:
        raise ValueError("context reference is required")

    _reject_filesystem_path_context_ref(normalized)

    if normalized.startswith("@") or _SPAWN_REF_RE.fullmatch(normalized):
        spawn_id = resolve_spawn_reference(project_root, normalized)
        spawn_row = read_spawn_row(project_root, spawn_id)
        if spawn_row is None:
            raise ValueError(f"Spawn '{spawn_id}' not found")
        return _spawn_context_ref(spawn_row, project_root)
    if CHAT_REF_RE.fullmatch(normalized) or _is_tracked_session(project_root, normalized):
        runtime_root = resolve_runtime_root_for_read(project_root)
        session = (
            session_store.chat_record(runtime_root, normalized)
            if runtime_root is not None
            else None
        )
        context_ref = _session_context_from(project_root, normalized, session)
        if context_ref is not None:
            if context_ref.primary_spawn_id is None and not (
                session and (session.harness_session_id or "").strip()
            ):
                raise ValueError(
                    f"Session '{normalized}' exists but no transcript is available yet "
                    "(no harness session id recorded)."
                )
            return context_ref
        raise ValueError(f"No primary spawn found for session '{normalized}'")

    runtime_root = resolve_runtime_root_for_read(project_root)
    session = (
        session_store.resolve_session_ref(runtime_root, normalized)
        if runtime_root is not None
        else None
    )
    if session is not None:
        context_ref = _session_context_from(project_root, session.chat_id, session)
        assert context_ref is not None
        return context_ref

    spawn_id = resolve_spawn_reference(project_root, normalized)
    row = read_spawn_row(project_root, spawn_id)
    if row is None:
        raise ValueError(f"Spawn '{spawn_id}' not found")
    return _spawn_context_ref(row, project_root)


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
        chat_id=row.chat_id,
        history_id=row.history_id,
        work_id=(row.work_id or "").strip() or None,
        task_cwd=(row.task_cwd or "").strip() or None,
    )


def _session_context_ref(primary_row: SpawnRecord, project_root: Path) -> SessionContextRef:
    chat_id = (primary_row.chat_id or "").strip()
    if not chat_id:
        raise ValueError(f"Primary spawn '{primary_row.id}' has no associated session")
    runtime_root = resolve_runtime_root_for_read(project_root)
    if runtime_root is None:
        raise ValueError(f"Primary spawn '{primary_row.id}' has no runtime state")
    work_id = (
        session_store.get_session_active_work_id(runtime_root, chat_id) or ""
    ).strip() or None
    return SessionContextRef(
        chat_id=chat_id,
        primary_spawn_id=primary_row.id,
        history_id=primary_row.history_id,
        status=primary_row.status,
        agent=primary_row.agent or "",
        model=primary_row.model or "",
        harness=primary_row.harness or "",
        harness_session_id=primary_row.harness_session_id,
        work_id=work_id,
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
