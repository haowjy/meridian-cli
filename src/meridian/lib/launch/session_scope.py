"""Session lifecycle context manager helpers."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
    harness: str,
    harness_session_id: str,
    model: str,
    chat_id: str | None = None,
    params: tuple[str, ...] = (),
    agent: str = "",
    agent_path: str = "",
    skills: tuple[str, ...] = (),
    skill_paths: tuple[str, ...] = (),
    forked_from_chat_id: str | None = None,
    execution_cwd: str | None = None,
    kind: Literal["primary", "spawn"] = "spawn",
    _start_session: Callable[..., str] = start_session,
    _stop_session: Callable[[Path, str], None] = stop_session,
    _update_session_harness_id: Callable[[Path, str, str], None] = update_session_harness_id,
) -> Generator[ManagedSession, None, None]:
    resolved_chat_id = _start_session(
        runtime_root,
        harness=harness,
        harness_session_id=harness_session_id,
        model=model,
        chat_id=chat_id,
        params=params,
        agent=agent,
        agent_path=agent_path,
        skills=skills,
        skill_paths=skill_paths,
        forked_from_chat_id=forked_from_chat_id,
        execution_cwd=execution_cwd,
        kind=kind,
    )

    def _record_harness_session_id(session_id: str) -> None:
        _update_session_harness_id(runtime_root, resolved_chat_id, session_id)

    try:
        yield ManagedSession(
            chat_id=resolved_chat_id,
            record_harness_session_id=_record_harness_session_id,
        )
    finally:
        _stop_session(runtime_root, resolved_chat_id)


__all__ = ["ManagedSession", "session_scope"]
