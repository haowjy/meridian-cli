"""Normalized chat event protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

EVENT_FAMILY_CHAT: Final = "chat"
EVENT_FAMILY_WORK: Final = "work"
EVENT_FAMILY_TURN: Final = "turn"
EVENT_FAMILY_CONTENT: Final = "content"
EVENT_FAMILY_ITEM: Final = "item"
EVENT_FAMILY_FILES: Final = "files"
EVENT_FAMILY_SPAWN: Final = "spawn"
EVENT_FAMILY_REQUEST: Final = "request"
EVENT_FAMILY_CHECKPOINT: Final = "checkpoint"
EVENT_FAMILY_MODEL: Final = "model"
EVENT_FAMILY_RUNTIME: Final = "runtime"
EVENT_FAMILY_EXTENSION: Final = "extension"

CHAT_STARTED: Final = "chat.started"
CHAT_EXITED: Final = "chat.exited"
CHAT_CONFIGURED: Final = "chat.configured"
CHAT_STATE_CHANGED: Final = "chat.state_changed"
USER_PROMPT: Final = "user.prompt"
TURN_STARTED: Final = "turn.started"
TURN_COMPLETED: Final = "turn.completed"
CONTENT_DELTA: Final = "content.delta"
RUNTIME_WARNING: Final = "runtime.warning"

EVENT_FAMILIES: Final = frozenset(
    {
        EVENT_FAMILY_CHAT,
        EVENT_FAMILY_WORK,
        EVENT_FAMILY_TURN,
        EVENT_FAMILY_CONTENT,
        EVENT_FAMILY_ITEM,
        EVENT_FAMILY_FILES,
        EVENT_FAMILY_SPAWN,
        EVENT_FAMILY_REQUEST,
        EVENT_FAMILY_CHECKPOINT,
        EVENT_FAMILY_MODEL,
        EVENT_FAMILY_RUNTIME,
        EVENT_FAMILY_EXTENSION,
    }
)


def utc_now_iso() -> str:
    """Return current UTC time as an ISO-8601 string with Z suffix."""

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ChatEvent:
    """One normalized event in a chat event stream."""

    type: str
    seq: int
    chat_id: str
    execution_id: str
    timestamp: str
    turn_id: str | None = None
    item_id: str | None = None
    request_id: str | None = None
    payload: dict[str, Any] = field(default_factory=lambda: {})
    harness_id: str | None = None


__all__ = [
    "CHAT_CONFIGURED",
    "CHAT_EXITED",
    "CHAT_STARTED",
    "CHAT_STATE_CHANGED",
    "CONTENT_DELTA",
    "EVENT_FAMILIES",
    "EVENT_FAMILY_CHAT",
    "EVENT_FAMILY_CHECKPOINT",
    "EVENT_FAMILY_CONTENT",
    "EVENT_FAMILY_EXTENSION",
    "EVENT_FAMILY_FILES",
    "EVENT_FAMILY_ITEM",
    "EVENT_FAMILY_MODEL",
    "EVENT_FAMILY_REQUEST",
    "EVENT_FAMILY_RUNTIME",
    "EVENT_FAMILY_SPAWN",
    "EVENT_FAMILY_TURN",
    "EVENT_FAMILY_WORK",
    "RUNTIME_WARNING",
    "TURN_COMPLETED",
    "TURN_STARTED",
    "USER_PROMPT",
    "ChatEvent",
    "utc_now_iso",
]
