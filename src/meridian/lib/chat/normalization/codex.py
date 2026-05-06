"""Codex HarnessEvent to ChatEvent normalization."""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

from meridian.lib.chat.normalization.common import (
    as_dict,
    as_str,
    canonical_item_type,
    extract_files,
)
from meridian.lib.chat.normalization.synthetic import is_turn_boundary_event
from meridian.lib.chat.protocol import (
    CONTENT_DELTA,
    TURN_COMPLETED,
    TURN_STARTED,
    ChatEvent,
    utc_now_iso,
)
from meridian.lib.harness.connections.base import HarnessEvent

HARNESS_ID = "codex"
ITEM_STARTED = "item.started"
ITEM_UPDATED = "item.updated"
ITEM_COMPLETED = "item.completed"
FILES_PERSISTED = "files.persisted"
REQUEST_OPENED = "request.opened"
REQUEST_RESOLVED = "request.resolved"
USER_INPUT_REQUESTED = "user_input.requested"
RUNTIME_WARNING = "runtime.warning"


class CodexNormalizer:
    """Stateful Codex JSON-RPC stream normalizer for one backing execution."""

    def __init__(self, chat_id: str, execution_id: str) -> None:
        self._chat_id = chat_id
        self._execution_id = execution_id
        self._turn_id: str | None = None
        self._active_raw_turn_id: str | None = None
        self._completed_raw_turn_ids: set[str] = set()
        self._completed_for_turn = False
        self._assistant_deltas_by_item: set[str] = set()

    def reset(self) -> None:
        self._turn_id = None
        self._active_raw_turn_id = None
        self._completed_raw_turn_ids.clear()
        self._completed_for_turn = False
        self._assistant_deltas_by_item.clear()

    def normalize(self, event: HarnessEvent) -> list[ChatEvent]:
        match event.event_type:
            case "turn/started":
                return [self._turn_started(event)]
            case "turn/completed":
                completed = self._turn_completed(event)
                return [completed] if completed is not None else []
            case value if is_turn_boundary_event(value):
                completed = self._synthetic_turn_completed(event)
                return [completed] if completed is not None else []
            case "item/tool/started" | "item/started":
                self._bind_turn_from_payload(event.payload)
                return [self._item_event(ITEM_STARTED, event)]
            case "item/tool/updated" | "item/updated":
                self._bind_turn_from_payload(event.payload)
                return [self._item_event(ITEM_UPDATED, event), *self._file_events(event)]
            case "item/tool/completed" | "item/completed":
                self._bind_turn_from_payload(event.payload)
                return self._item_completed(event)
            case "item/agentMessage/delta":
                self._bind_turn_from_payload(event.payload)
                return [self._assistant_delta(event)]
            case "agent_message_chunk" | "content/delta" | "agent/message/delta":
                self._bind_turn_from_payload(event.payload)
                return [self._content_delta(event, "assistant_text")]
            case "agent_thought_chunk" | "reasoning/delta" | "agent/thought/delta":
                self._bind_turn_from_payload(event.payload)
                return [self._content_delta(event, "reasoning_text")]
            case "request.opened" | "request/opened":
                return [self._request_event(REQUEST_OPENED, event)]
            case "request.resolved" | "request/resolved":
                return [self._request_event(REQUEST_RESOLVED, event)]
            case "user_input.requested" | "user_input/requested":
                return [self._request_event(USER_INPUT_REQUESTED, event)]
            case "files/persisted" | "files.persisted" | "file/write" | "file/persisted":
                return self._file_events(event) or [
                    self._event(FILES_PERSISTED, event, payload=dict(event.payload))
                ]
            case "warning/unsupportedServerRequest" | "warning/approvalRejected":
                return [self._event(RUNTIME_WARNING, event, payload=dict(event.payload))]
            case _:
                return []

    def _turn_started(self, event: HarnessEvent) -> ChatEvent:
        turn_id = _extract_turn_id(event.payload) or f"turn-{uuid4()}"
        self._turn_id = turn_id
        self._active_raw_turn_id = _extract_turn_id(event.payload)
        self._completed_for_turn = False
        self._assistant_deltas_by_item.clear()
        payload: dict[str, Any] = {}
        for key in ("model", "session_id"):
            if key in event.payload:
                payload[key] = event.payload[key]
        thread_id = _extract_thread_id(event.payload)
        if thread_id is not None:
            payload["thread_id"] = thread_id
        return self._event(TURN_STARTED, event, payload=payload)

    def _turn_completed(self, event: HarnessEvent) -> ChatEvent | None:
        turn_id = _extract_turn_id(event.payload)
        if turn_id is not None and turn_id in self._completed_raw_turn_ids:
            return None
        if turn_id is not None and self._active_raw_turn_id not in {None, turn_id}:
            return None
        if self._completed_for_turn:
            return None
        if self._turn_id is None:
            self._turn_id = turn_id or f"turn-{uuid4()}"
        if turn_id is not None and self._active_raw_turn_id is None:
            self._active_raw_turn_id = turn_id
        payload = _turn_payload(event.payload)
        chat_event = self._event(TURN_COMPLETED, event, payload=payload)
        if turn_id is not None:
            self._completed_raw_turn_ids.add(turn_id)
        self._turn_id = None
        self._active_raw_turn_id = None
        self._completed_for_turn = True
        self._assistant_deltas_by_item.clear()
        return chat_event

    def _synthetic_turn_completed(self, event: HarnessEvent) -> ChatEvent | None:
        if self._completed_for_turn or self._turn_id is None:
            return None
        return self._turn_completed(event)

    def _item_completed(self, event: HarnessEvent) -> list[ChatEvent]:
        item_event = self._item_event(ITEM_COMPLETED, event)
        events = [item_event, *self._file_events(event)]
        item = _item_payload(event.payload)
        item_id = item_event.item_id
        item_type = as_str(item.get("type"))
        text = as_str(item.get("text"))
        if (
            item_type == "agentMessage"
            and text
            and item_id is not None
            and item_id not in self._assistant_deltas_by_item
        ):
            events.append(
                self._event(
                    CONTENT_DELTA,
                    event,
                    item_id=item_id,
                    payload={"stream_kind": "assistant_text", "text": text},
                )
            )
            self._assistant_deltas_by_item.add(item_id)
        return events

    def _assistant_delta(self, event: HarnessEvent) -> ChatEvent:
        item_id = _extract_item_id(event.payload)
        if item_id is not None:
            self._assistant_deltas_by_item.add(item_id)
        return self._event(
            CONTENT_DELTA,
            event,
            item_id=item_id,
            payload={"stream_kind": "assistant_text", "text": _text_from_payload(event.payload)},
        )

    def _bind_turn_from_payload(self, payload: dict[str, object]) -> None:
        turn_id = _extract_turn_id(payload)
        if turn_id is not None and self._turn_id is None:
            self._turn_id = turn_id
        if turn_id is not None and self._active_raw_turn_id is None:
            self._active_raw_turn_id = turn_id

    def _item_event(self, event_type: str, event: HarnessEvent) -> ChatEvent:
        item = _item_payload(event.payload)
        item_id = _extract_item_id(event.payload) or _extract_item_id(item) or f"item-{uuid4()}"
        raw_type = (
            as_str(item.get("type"))
            or as_str(event.payload.get("tool_type"))
            or as_str(event.payload.get("type"))
        )
        name = as_str(item.get("name")) or as_str(event.payload.get("name"))
        payload = dict(event.payload)
        payload["item_type"] = canonical_item_type(raw_type, name)
        if raw_type is not None:
            payload["raw_type"] = raw_type
        if name is not None:
            payload["name"] = name
        return self._event(event_type, event, item_id=item_id, payload=payload)

    def _content_delta(self, event: HarnessEvent, stream_kind: str) -> ChatEvent:
        return self._event(
            CONTENT_DELTA,
            event,
            item_id=_extract_item_id(event.payload),
            payload={"stream_kind": stream_kind, "text": _text_from_payload(event.payload)},
        )

    def _request_event(self, event_type: str, event: HarnessEvent) -> ChatEvent:
        request_id = as_str(event.payload.get("request_id")) or as_str(event.payload.get("id"))
        payload = dict(event.payload)
        if event_type == USER_INPUT_REQUESTED:
            payload.setdefault("request_type", "user_input")
        return self._event(event_type, event, request_id=request_id, payload=payload)

    def _file_events(self, event: HarnessEvent) -> list[ChatEvent]:
        files = extract_files(event.payload)
        if not files:
            return []
        return [self._event(FILES_PERSISTED, event, payload={"files": files})]

    def _event(
        self,
        event_type: str,
        event: HarnessEvent,
        *,
        item_id: str | None = None,
        request_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> ChatEvent:
        return ChatEvent(
            type=event_type,
            seq=0,
            chat_id=self._chat_id,
            execution_id=self._execution_id,
            timestamp=utc_now_iso(),
            turn_id=self._turn_id,
            item_id=item_id,
            request_id=request_id,
            payload=payload or {},
            harness_id=event.harness_id,
        )


def _item_payload(payload: dict[str, object]) -> dict[str, object]:
    item = payload.get("item") or payload.get("tool")
    return cast("dict[str, object]", item) if isinstance(item, dict) else payload


def _turn_payload(payload: dict[str, object]) -> dict[str, Any]:
    turn = as_dict(payload.get("turn")) or payload
    result: dict[str, Any] = {}
    status = as_str(turn.get("status")) or as_str(payload.get("status"))
    if status is not None:
        result["status"] = status
    exit_code = payload.get("exit_code")
    if isinstance(exit_code, int):
        result["exit_code"] = exit_code
    error = turn.get("error") if "error" in turn else payload.get("error")
    if error is not None:
        result["error"] = error
    if "usage" in payload:
        result["usage"] = payload["usage"]
    duration_ms = payload.get("duration_ms")
    if not isinstance(duration_ms, int):
        duration_ms = turn.get("durationMs")
    if isinstance(duration_ms, int):
        result["duration_ms"] = duration_ms
    if "synthetic" in payload:
        result["synthetic"] = payload["synthetic"]
    thread_id = _extract_thread_id(payload)
    if thread_id is not None:
        result["thread_id"] = thread_id
    return result


def _text_from_payload(payload: dict[str, object]) -> str:
    for key in ("text", "delta", "content"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    message = as_dict(payload.get("message"))
    if message is not None:
        return _text_from_payload(message)
    item = as_dict(payload.get("item"))
    if item is not None:
        return _text_from_payload(item)
    return ""


def _extract_turn_id(payload: dict[str, object]) -> str | None:
    turn = as_dict(payload.get("turn"))
    if turn is not None and as_str(turn.get("id")) is not None:
        return as_str(turn.get("id"))
    return (
        as_str(payload.get("turn_id"))
        or as_str(payload.get("turnId"))
        or as_str(payload.get("id"))
    )


def _extract_thread_id(payload: dict[str, object]) -> str | None:
    thread = as_dict(payload.get("thread"))
    if thread is not None and as_str(thread.get("id")) is not None:
        return as_str(thread.get("id"))
    return as_str(payload.get("thread_id")) or as_str(payload.get("threadId"))


def _extract_item_id(payload: dict[str, object]) -> str | None:
    return (
        as_str(payload.get("item_id"))
        or as_str(payload.get("itemId"))
        or as_str(payload.get("id"))
    )


__all__ = ["CodexNormalizer"]
