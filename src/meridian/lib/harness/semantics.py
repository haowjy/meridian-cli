"""Pure semantic interpretation helpers for harness events."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.common import extract_codex_thread_id

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


def _codex_turn_completes_session(
    event: HarnessEvent,
    *,
    codex_main_thread_id: str | None,
) -> bool:
    if event.harness_id != HarnessId.CODEX.value or event.event_type != "turn/completed":
        return False
    if codex_main_thread_id is None:
        return True
    event_thread_id = extract_codex_thread_id(event.payload)
    if event_thread_id is None:
        return True
    return event_thread_id == codex_main_thread_id


def terminal_outcome(
    event: HarnessEvent,
    *,
    codex_main_thread_id: str | None = None,
) -> TerminalEventOutcome | None:
    """Classify whether a harness event completes a spawn drain."""

    if _codex_turn_completes_session(
        event,
        codex_main_thread_id=codex_main_thread_id,
    ):
        return TerminalEventOutcome(status="succeeded", exit_code=0)

    if event.event_type == "error/connectionClosed":
        error = _stringify_terminal_error(event.payload.get("message")) or "connection_closed"
        return TerminalEventOutcome(
            status="failed",
            exit_code=1,
            error=error,
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

    if event.harness_id == HarnessId.CURSOR.value and event.event_type == "result":
        if bool(event.payload.get("is_error")):
            error = (
                _stringify_terminal_error(event.payload.get("result"))
                or _stringify_terminal_error(event.payload.get("error"))
                or "cursor_result_error"
            )
            return TerminalEventOutcome(status="failed", exit_code=1, error=error)

        subtype = str(event.payload.get("subtype", "")).strip().lower()
        if subtype in {"", "success"}:
            return TerminalEventOutcome(status="succeeded", exit_code=0)

        error = _stringify_terminal_error(event.payload.get("result"))
        return TerminalEventOutcome(
            status="failed",
            exit_code=1,
            error=error or f"cursor_result_{subtype}",
        )

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
                if stop_reason in {"abort", "aborted", "cancel", "cancelled", "canceled"}:
                    return TerminalEventOutcome(
                        status="cancelled",
                        exit_code=130,
                        error="cancelled",
                    )
                return TerminalEventOutcome(status="succeeded", exit_code=0)
        return TerminalEventOutcome(status="succeeded", exit_code=0)

    if event.harness_id == HarnessId.PI.value and event.event_type == "response":
        command = str(event.payload.get("command", "")).strip().lower()
        if command == "prompt" and event.payload.get("success") is False:
            error = _stringify_terminal_error(event.payload.get("error")) or "pi_prompt_rejected"
            return TerminalEventOutcome(status="failed", exit_code=1, error=error)

    return None


def activity_transition(
    event: HarnessEvent,
    *,
    codex_main_thread_id: str | None = None,
) -> ActivityState | None:
    """Return primary UI activity transition caused by a harness event."""

    if event.event_type in {
        "turn/started",  # Codex
        "agent_message_chunk",  # OpenCode: assistant text streaming
        "agent_thought_chunk",  # OpenCode: reasoning streaming
        "tool_call",  # OpenCode: tool invocation
        "tool_call_update",  # OpenCode: tool result
    }:
        return "turn_active"
    if event.event_type == "turn/completed":
        if _codex_turn_completes_session(
            event,
            codex_main_thread_id=codex_main_thread_id,
        ):
            return "idle"
        return None
    if event.event_type == "session.idle":
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


def clears_signal(
    event: HarnessEvent,
    *,
    codex_main_thread_id: str | None = None,
) -> bool:
    """Return whether an event clears a pending user signal for its harness."""

    if event.harness_id in {HarnessId.CLAUDE.value, HarnessId.CURSOR.value}:
        return event.event_type == "result"
    if event.harness_id == HarnessId.CODEX.value:
        return _codex_turn_completes_session(
            event,
            codex_main_thread_id=codex_main_thread_id,
        )
    if event.harness_id == HarnessId.OPENCODE.value:
        return event.event_type in {"session.idle", "session.error"}
    if event.harness_id == HarnessId.PI.value:
        return event.event_type == "agent_end"
    return False


@dataclass
class CodexDrainThreadTracker:
    """Track the main Codex thread for thread-aware drain classification."""

    main_thread_id: str | None = field(default=None)

    def observe(self, event: HarnessEvent) -> None:
        if event.harness_id != HarnessId.CODEX.value or event.event_type != "turn/started":
            return
        if self.main_thread_id is not None:
            return
        thread_id = extract_codex_thread_id(event.payload)
        if thread_id:
            self.main_thread_id = thread_id

    def terminal_outcome(self, event: HarnessEvent) -> TerminalEventOutcome | None:
        self.observe(event)
        return terminal_outcome(event, codex_main_thread_id=self.main_thread_id)


__all__ = [
    "ActivityState",
    "CodexDrainThreadTracker",
    "TerminalEventOutcome",
    "activity_transition",
    "clears_signal",
    "terminal_outcome",
]
