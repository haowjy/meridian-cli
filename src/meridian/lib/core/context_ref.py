"""Resolved ``--from`` context payloads, shared by launch requests and the spawn resolver."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SpawnContextRef(BaseModel):
    """Resolved context payload sourced from one prior spawn."""

    model_config = ConfigDict(frozen=True)

    ref_kind: Literal["spawn"] = "spawn"
    spawn_id: str
    status: str
    agent: str
    desc: str
    model: str
    harness: str
    report_text: str | None = None
    harness_session_id: str | None = None
    chat_id: str | None = None
    history_id: UUID | None = None
    work_id: str | None = None
    task_cwd: str | None = None


class SessionContextRef(BaseModel):
    """Resolved context payload sourced from one prior chat/session."""

    model_config = ConfigDict(frozen=True)

    ref_kind: Literal["session"] = "session"
    chat_id: str
    primary_spawn_id: str | None = None
    history_id: UUID | None = None
    status: str
    agent: str
    model: str
    harness: str
    harness_session_id: str | None = None
    work_id: str | None = None
    task_cwd: str | None = None


type ContextRef = SpawnContextRef | SessionContextRef


__all__ = ["ContextRef", "SessionContextRef", "SpawnContextRef"]
