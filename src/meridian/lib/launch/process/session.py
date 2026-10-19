"""Session bookkeeping helpers for process launches."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from meridian.lib.launch.context import LaunchContext
from meridian.lib.launch.request import SpawnRequest
from meridian.lib.launch.types import PrimarySessionMetadata, SessionMode


def build_session_metadata(request: SpawnRequest) -> PrimarySessionMetadata:
    return PrimarySessionMetadata(
        harness=request.harness or "",
        model=request.model or "",
        agent=request.agent or "",
        agent_path=request.agent_metadata.get("session_agent_path") or "",
        skills=request.skills,
        skill_paths=request.skill_paths,
    )


def resolve_primary_session_mode(context: LaunchContext) -> SessionMode:
    raw_mode = (context.resolved_request.session.primary_session_mode or "").strip()
    if not raw_mode:
        return SessionMode.FRESH
    try:
        return SessionMode(raw_mode)
    except ValueError:
        return SessionMode.FRESH


def resolve_attached_work_id(
    *,
    project_root: Path,
    runtime_root: Path,
    chat_id: str,
    explicit_work_id: str | None,
    resume_chat_id: str | None,
    get_session_active_work_id_fn: Callable[[Path, str], str | None],
    activate_work_fn: Callable[[Path, str, str], None],
) -> str | None:
    preserved_work_id = None
    if explicit_work_id is None and resume_chat_id is not None:
        preserved_work_id = get_session_active_work_id_fn(runtime_root, resume_chat_id)

    attached_work_id = get_session_active_work_id_fn(runtime_root, chat_id)
    if attached_work_id is None:
        attached_work_id = explicit_work_id or preserved_work_id
        if attached_work_id is not None:
            activate_work_fn(project_root, chat_id, attached_work_id)
    return attached_work_id


__all__ = [
    "build_session_metadata",
    "resolve_attached_work_id",
    "resolve_primary_session_mode",
]
