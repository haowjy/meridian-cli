"""Session lifecycle context manager helpers."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from meridian.lib.core.process_cleanup import reclaim_session_owned_scopes_for_chat
from meridian.lib.launch.request import SessionRequest
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.state.session_store import (
    start_session,
    stop_session,
    update_session_harness_id,
)


@dataclass(frozen=True)
class ManagedSession:
    chat_id: str
    record_harness_session_id: Callable[[str], None]


@contextmanager
def session_scope(
    *,
    runtime_root: Path,
    metadata: PrimarySessionMetadata,
    request: SessionRequest,
    harness_session_id: str,
    chat_id: str | None = None,
    params: tuple[str, ...] = (),
    control_root: str | None = None,
    task_cwd: str | None = None,
    execution_cwd: str | None = None,
    kind: Literal["primary", "spawn"] = "spawn",
    spawn_id: str | None = None,
    _start_session: Callable[..., str] = start_session,
    _stop_session: Callable[[Path, str], None] = stop_session,
    _update_session_harness_id: Callable[[Path, str, str], None] = update_session_harness_id,
    _reclaim_session_scopes: Callable[[Path, str], object] = reclaim_session_owned_scopes_for_chat,
) -> Generator[ManagedSession, None, None]:
    resolved_chat_id = _start_session(
        runtime_root,
        harness=metadata.harness,
        harness_session_id=harness_session_id,
        model=metadata.model,
        chat_id=chat_id,
        params=params,
        agent=metadata.agent,
        agent_path=metadata.agent_path,
        skills=metadata.skills,
        skill_paths=metadata.skill_paths,
        forked_from_chat_id=request.forked_from_chat_id,
        control_root=control_root,
        task_cwd=task_cwd,
        execution_cwd=execution_cwd,
        kind=kind,
        spawn_id=spawn_id,
    )

    def _record_harness_session_id(session_id: str) -> None:
        _update_session_harness_id(runtime_root, resolved_chat_id, session_id)

    try:
        yield ManagedSession(
            chat_id=resolved_chat_id,
            record_harness_session_id=_record_harness_session_id,
        )
    finally:
        try:
            _stop_session(runtime_root, resolved_chat_id)
        finally:
            _reclaim_session_scopes(runtime_root, resolved_chat_id)


__all__ = ["ManagedSession", "session_scope"]
