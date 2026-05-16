"""Pure semantic interpretation helpers for harness events."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.types import HarnessId

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessEvent


ActivityState = Literal["turn_active", "idle"]


@dataclass(frozen=True)
class TerminalEventOutcome:
    status: SpawnStatus
    exit_code: int
    error: str | None = None


def _stringify_terminal_error(error: object) -> str | None:
    if error is None:
        return None
    if isinstance(error, str):
        normalized = error.strip()
        return normalized or None
    try:
        rendered = json.dumps(error, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(error)
    normalized = rendered.strip()
    return normalized or None


def terminal_outcome(event: HarnessEvent) -> TerminalEventOutcome | None:
    """Classify whether a harness event completes a spawn drain."""

    if event.harness_id == HarnessId.CODEX.value and event.event_type == "turn/completed":
        return TerminalEventOutcome(status="succeeded", exit_code=0)

    if event.event_type == "error/connectionClosed":
        return TerminalEventOutcome(
            status="failed",
            exit_code=1,
            error="connection_closed",
        )

    if event.harness_id == HarnessId.CLAUDE.value and event.event_type == "result":
        if bool(event.payload.get("is_error")):
            error = (
                _stringify_terminal_error(event.payload.get("result"))
                or _stringify_terminal_error(event.payload.get("error"))
                or "claude_result_error"
            )
            return TerminalEventOutcome(status="failed", exit_code=1, error=error)

        subtype = str(event.payload.get("subtype", "")).strip().lower()
        terminal_reason = str(event.payload.get("terminal_reason", "")).strip().lower()
        if subtype in {"", "success"} and terminal_reason in {"", "completed"}:
            return TerminalEventOutcome(status="succeeded", exit_code=0)
        if terminal_reason == "completed":
            return TerminalEventOutcome(status="succeeded", exit_code=0)

        error = _stringify_terminal_error(event.payload.get("result"))
        if subtype not in {"", "success"}:
            error = error or f"claude_result_{subtype}"
        elif terminal_reason:
            error = error or f"claude_terminal_{terminal_reason}"
        else:
            error = error or "claude_result_unknown"
        return TerminalEventOutcome(status="failed", exit_code=1, error=error)

    if event.harness_id == HarnessId.OPENCODE.value:
        if event.event_type == "session.idle":
            return TerminalEventOutcome(status="succeeded", exit_code=0)

        if event.event_type == "session.error":
            properties = event.payload.get("properties")
            error = (
                _stringify_terminal_error(cast("dict[str, object]", properties))
                if isinstance(properties, dict)
                else _stringify_terminal_error(event.payload.get("error"))
            )
            return TerminalEventOutcome(
                status="failed",
                exit_code=1,
                error=error or "opencode_session_error",
            )

    if event.harness_id == HarnessId.PI.value and event.event_type == "agent_end":
        messages_obj = event.payload.get("messages")
        if isinstance(messages_obj, list):
            for message_obj in reversed(cast("list[object]", messages_obj)):
                if not isinstance(message_obj, dict):
                    continue
                message = cast("dict[str, object]", message_obj)
                if str(message.get("role", "")).strip().lower() != "assistant":
                    continue
                stop_reason = str(message.get("stopReason", "")).strip().lower()
                if stop_reason == "error":
                    return TerminalEventOutcome(
                        status="failed",
                        exit_code=1,
                        error="pi_stop_error",
                    )
                return TerminalEventOutcome(status="succeeded", exit_code=0)
        return TerminalEventOutcome(status="succeeded", exit_code=0)

    return None


def activity_transition(event: HarnessEvent) -> ActivityState | None:
    """Return primary UI activity transition caused by a harness event."""

    if event.event_type in {
        "turn/started",  # Codex
        "agent_message_chunk",  # OpenCode: assistant text streaming
        "agent_thought_chunk",  # OpenCode: reasoning streaming
        "tool_call",  # OpenCode: tool invocation
        "tool_call_update",  # OpenCode: tool result
    }:
        return "turn_active"
    if event.event_type in {"turn/completed", "session.idle"}:
        return "idle"
    if event.harness_id == HarnessId.PI.value:
        if event.event_type in {
            "agent_start",
            "turn_start",
            "message_start",
            "message_update",
            "tool_execution_start",
            "tool_execution_update",
        }:
            return "turn_active"
        if event.event_type in {"turn_end", "agent_end"}:
            return "idle"
    return None


def clears_signal(event: HarnessEvent) -> bool:
    """Return whether an event clears a pending user signal for its harness."""

    if event.harness_id == HarnessId.CLAUDE.value:
        return event.event_type == "result"
    if event.harness_id == HarnessId.CODEX.value:
        return event.event_type == "turn/completed"
    if event.harness_id == HarnessId.OPENCODE.value:
        return event.event_type in {"session.idle", "session.error"}
    if event.harness_id == HarnessId.PI.value:
        return event.event_type == "agent_end"
    return False


__all__ = [
    "ActivityState",
    "TerminalEventOutcome",
    "activity_transition",
    "clears_signal",
    "terminal_outcome",
]
