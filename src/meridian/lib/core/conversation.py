# TODO: delete this module. Nothing in src/ or tests/ imports it;
# lib/harness/transcript.py superseded it.
"""Unified conversation model for cross-harness history representation."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ToolCall(BaseModel):
    """One tool invocation within a conversation turn."""

    model_config = ConfigDict(frozen=True)

    tool_name: str
    input: dict[str, Any]
    output: str | None = None


class ConversationTurn(BaseModel):
    """One message in a conversation."""

    model_config = ConfigDict(frozen=True)

    role: Literal["user", "assistant", "system"]
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    timestamp: str | None = None


class Conversation(BaseModel):
    """Unified conversation extracted from a spawn's harness output."""

    model_config = ConfigDict(frozen=True)

    spawn_id: str
    harness: str
    turns: tuple[ConversationTurn, ...]
