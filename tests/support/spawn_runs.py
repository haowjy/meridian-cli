"""Concise builders for spawn run fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from meridian.lib.core.domain import TerminalSpawnStatus
from meridian.lib.core.refs import parse_ref, spawn_of
from meridian.lib.core.spawn_lifecycle import SpawnReservation
from meridian.lib.core.types import RunId
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.state import session_store, spawn_store


def bind_run_to_started_session(root: Path, run_id: RunId, chat_id: str) -> None:
    """Bind ``run_id`` to the session generation just started for ``chat_id``."""
    generation = session_store.get_session_record(root, chat_id)
    assert generation is not None
    spawn_store.bind_run_generation(
        root,
        run_id,
        chat_id=chat_id,
        session_instance_id=generation.session_instance_id,
        forked_from_history_id=generation.forked_from_history_id,
    )


def reservation(
    *,
    chat_id: str = "c1",
    prompt: str = "again",
    harness: str = "codex",
    model: str = "test",
    agent: str = "",
    agent_path: str = "",
    skills: tuple[str, ...] = (),
    skill_paths: tuple[str, ...] = (),
    **overrides: Any,
) -> SpawnReservation:
    """Build a run reservation while keeping session metadata out of each test."""
    fields: dict[str, Any] = {
        "chat_id": chat_id,
        "prompt": prompt,
        "session_metadata": PrimarySessionMetadata(
            harness=harness,
            model=model,
            agent=agent,
            agent_path=agent_path,
            skills=skills,
            skill_paths=skill_paths,
        ),
    }
    fields.update(overrides)
    return SpawnReservation(**fields)


def seed_run(
    root: Path,
    key: str | None = "p1",
    *,
    bound: bool = False,
    status: TerminalSpawnStatus | None = "succeeded",
    parent: str | None = None,
    chat_id: str = "c1",
    owner_chat_id: str | None = None,
    model: str = "test",
    agent: str = "coder",
    harness: str = "codex",
    prompt: str = "hello",
    started_at: str | None = None,
    runner_pid: int | None = None,
    harness_session_id: str = "native-1",
    native_store: str | None = None,
) -> RunId:
    """Publish one run, optionally bind a session, and optionally finalize it."""
    parent_run_id = parse_ref(parent, row_key=True) if parent is not None else None
    effective_runner_pid = (
        runner_pid if runner_pid is not None or status is None else 999_999_999
    )
    run_id = spawn_store.start_spawn(
        root,
        spawn_id=key,
        chat_id=chat_id,
        owner_chat_id=owner_chat_id,
        parent_id=spawn_of(parent_run_id) if parent_run_id is not None else None,
        parent_run_id=parent_run_id,
        model=model,
        agent=agent,
        harness=harness,
        prompt=prompt,
        started_at=started_at,
        runner_pid=effective_runner_pid,
    )
    if bound:
        session_store.start_session(
            root,
            harness=harness,
            harness_session_id=harness_session_id,
            model=model,
            chat_id=chat_id,
            spawn_id=run_id,
            native_store=native_store,
        )
        bind_run_to_started_session(root, run_id, chat_id)
        session_store.stop_session(root, chat_id)
    if status is not None:
        spawn_store.finalize_spawn(
            root,
            run_id,
            status=status,
            exit_code=0 if status == "succeeded" else 1,
            origin="runner",
        )
    return run_id
