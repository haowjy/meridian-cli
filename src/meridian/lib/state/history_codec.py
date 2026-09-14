"""Portable transcript identity; mutable lifecycle facts stay in record files."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from meridian.lib.state.spawn.model import SpawnRecord

if TYPE_CHECKING:
    from meridian.lib.state.session_store import SessionRecord


def canonical_time(value: str) -> str:
    return (
        datetime.fromisoformat(value).astimezone(UTC).isoformat(timespec="microseconds")
        if value
        else ""
    )


def last_activity(state: SpawnRecord, session: SessionRecord | None, transcript_stamp: str) -> str:
    values = [
        state.started_at or "",
        state.terminal.finished_at if state.terminal else "",
        transcript_stamp,
    ]
    if session is not None:
        values.extend((session.started_at, session.stopped_at or ""))
    return max(canonical_time(value) for value in values)


class TranscriptOrigin(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str
    spawn_id: str
    chat_id: str | None
    owner_chat_id: str | None
    parent_spawn_id: str | None


class HistoryRelationships(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    parent_history_id: UUID | None = None
    owner_history_id: UUID | None = None
    forked_from_history_id: UUID | None = None


class TranscriptHeader(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    record: Literal["meridian.transcript"] = "meridian.transcript"
    version: Literal[2] = 2
    history_id: UUID
    origin: TranscriptOrigin
    kind: Literal["primary", "spawn"]
    created_at: str
    relationships: HistoryRelationships


def transcript_header(state: SpawnRecord, project_id: str) -> TranscriptHeader:
    if state.history_id is None:
        raise ValueError("Portable transcript requires file-authoritative history identity")
    return TranscriptHeader(
        history_id=state.history_id,
        origin=TranscriptOrigin(
            project_id=project_id,
            spawn_id=state.id,
            chat_id=state.chat_id,
            owner_chat_id=state.owner_chat_id,
            parent_spawn_id=state.parent_id,
        ),
        kind="primary" if state.kind == "primary" else "spawn",
        created_at=state.started_at or "",
        relationships=HistoryRelationships(
            parent_history_id=state.parent_history_id,
            owner_history_id=state.owner_history_id,
            forked_from_history_id=state.forked_from_history_id,
        ),
    )


def current_attempt_lines(raw: str) -> list[str]:
    """Lifecycle-only view; transcript rendering must retain earlier attempts."""
    lines: list[str] = []
    for line in reversed(raw.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if isinstance(event, dict):
            if event.get("event_type") == "meridian.attempt.completed":
                break
            if event.get("record") == "meridian.transcript":
                continue
        lines.append(line)
    return list(reversed(lines))
