"""Central ChatCommand dispatch."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from meridian.lib.chat.command_invariants import NoActiveExecutionError, require_active_execution
from meridian.lib.chat.commands import COMMAND_APPROVE, ChatCommand, CommandResult
from meridian.lib.chat.protocol import USER_PROMPT, ChatEvent, utc_now_iso
from meridian.lib.chat.session_service import (
    ChatClosedError,
    ChatSessionService,
    ConcurrentPromptError,
)
from meridian.lib.telemetry import emit_telemetry, make_error_data

if TYPE_CHECKING:
    from meridian.lib.chat.checkpoint import CheckpointService
    from meridian.lib.chat.event_pipeline import ChatEventPipeline


class ChatCommandHandler:
    """Single dispatch point for all inbound chat commands."""

    def __init__(
        self,
        sessions: Mapping[str, ChatSessionService],
        pipelines: Mapping[str, ChatEventPipeline] | None = None,
        checkpoints: Mapping[str, CheckpointService] | None = None,
    ) -> None:
        self._sessions = sessions
        self._pipelines = pipelines or {}
        self._checkpoints = checkpoints or {}

    async def dispatch(self, command: ChatCommand) -> CommandResult:
        start = time.monotonic()
        caught: BaseException | None = None
        try:
            result = await self._dispatch(command)
        except ConcurrentPromptError as exc:
            caught = exc
            result = CommandResult(status="rejected", error="concurrent_prompt")
        except ChatClosedError as exc:
            caught = exc
            result = CommandResult(status="rejected", error="chat_closed")
        except NoActiveExecutionError as exc:
            caught = exc
            result = CommandResult(status="rejected", error="no_active_execution")
        except Exception as exc:
            caught = exc
            result = CommandResult(status="rejected", error=str(exc))
        _emit_command_dispatched(command, result, start, caught)
        return result

    async def _dispatch(self, command: ChatCommand) -> CommandResult:
        session = self._sessions.get(command.chat_id)
        if session is None:
            return CommandResult(status="rejected", error="chat_not_found")
        if session.state == "closed":
            return CommandResult(status="rejected", error="chat_closed")

        match command.type:
            case "prompt":
                text = command.payload.get("text")
                if not isinstance(text, str) or not text:
                    return CommandResult(
                        status="rejected",
                        error="invalid_command:missing_text",
                    )
                await session.prompt(text)
                await self._emit_user_prompt_event(session, command, text)
            case "cancel":
                await session.cancel()
            case "approve":
                await self._handle_approve(session, command)
            case "answer_input":
                await self._handle_answer_input(session, command)
            case "close":
                await session.close(self._pipelines.get(command.chat_id))
            case "revert":
                checkpoint = self._checkpoints.get(command.chat_id)
                if checkpoint is None:
                    return CommandResult(
                        status="rejected",
                        error="checkpoint_not_configured",
                    )
                commit_sha = _required_str(command.payload, "commit_sha")
                await checkpoint.revert_to_checkpoint(commit_sha)
            case "swap_model" | "swap_effort":
                # These commands stay schema-recognized so clients can
                # share one command vocabulary, but no current harness
                # supports runtime model/effort switching through the live
                # chat connection. Supporting them requires a harness
                # capability plus session/runtime code that safely applies
                # the switch to the active execution.
                return CommandResult(
                    status="rejected",
                    error="not_supported_by_current_harness",
                )
            case _:
                return CommandResult(
                    status="rejected",
                    error=f"unknown_command_type:{command.type}",
                )
        return CommandResult(status="accepted")

    async def _emit_user_prompt_event(
        self,
        session: ChatSessionService,
        command: ChatCommand,
        text: str,
    ) -> None:
        pipeline = self._pipelines.get(command.chat_id)
        handle = session.current_execution
        if pipeline is None or handle is None:
            return
        await pipeline.ingest(
            ChatEvent(
                type=USER_PROMPT,
                seq=0,
                chat_id=command.chat_id,
                execution_id=str(handle.spawn_id),
                timestamp=utc_now_iso(),
                payload={"text": text},
            )
        )

    async def _handle_approve(self, session: ChatSessionService, command: ChatCommand) -> None:
        handle = require_active_execution(session)
        request_id = _required_str(command.payload, "request_id")
        decision = _required_str(command.payload, "decision")
        raw_payload = command.payload.get("payload")
        if raw_payload is not None and not isinstance(raw_payload, dict):
            raise ValueError("invalid_command:payload_not_object")
        response_payload = _optional_object_dict(cast("object", raw_payload))
        await handle.respond_request(request_id, decision, response_payload)
        await self._emit_resolution_event(session, command, request_id, {"decision": decision})

    async def _handle_answer_input(self, session: ChatSessionService, command: ChatCommand) -> None:
        handle = require_active_execution(session)
        request_id = _required_str(command.payload, "request_id")
        raw_answers = command.payload.get("answers")
        if not isinstance(raw_answers, dict):
            raise ValueError("invalid_command:missing_answers")
        answers = _required_object_dict(cast("object", raw_answers))
        await handle.respond_user_input(request_id, answers)
        await self._emit_resolution_event(session, command, request_id, {"answers": answers})

    async def _emit_resolution_event(
        self,
        session: ChatSessionService,
        command: ChatCommand,
        request_id: str,
        payload: dict[str, Any],
    ) -> None:
        pipeline = self._pipelines.get(command.chat_id)
        handle = session.current_execution
        if pipeline is None or handle is None:
            return
        event_type = (
            "request.resolved" if command.type == COMMAND_APPROVE else "user_input.resolved"
        )
        await pipeline.ingest(
            ChatEvent(
                type=event_type,
                seq=0,
                chat_id=command.chat_id,
                execution_id=str(handle.spawn_id),
                timestamp=utc_now_iso(),
                request_id=request_id,
                payload={"command_id": command.command_id, **payload},
            )
        )


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid_command:missing_{key}")
    return value


def _optional_object_dict(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    return _required_object_dict(value)


def _required_object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("invalid_command:expected_object")
    typed_value = cast("dict[object, object]", value)
    return {str(key): item for key, item in typed_value.items()}


def _emit_command_dispatched(
    command: ChatCommand,
    result: CommandResult,
    start: float,
    exc: BaseException | None,
) -> None:
    data: dict[str, Any] = {
        "command_type": command.type,
        "result_status": result.status,
        "latency_ms": round((time.monotonic() - start) * 1000, 1),
    }
    severity = "info"
    if result.error is not None:
        data["error"] = result.error
        severity = "warning"
    if exc is not None:
        data.update(make_error_data(exc))
        severity = "error"
    emit_telemetry(
        "chat",
        "chat.command.dispatched",
        scope="chat.command_handler",
        ids={"chat_id": command.chat_id, "command_id": command.command_id},
        data=data,
        severity=severity,
    )


__all__ = ["ChatCommandHandler"]
