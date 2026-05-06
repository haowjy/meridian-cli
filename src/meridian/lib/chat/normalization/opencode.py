"""OpenCode HarnessEvent to ChatEvent normalization."""

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

ITEM_STARTED = "item.started"
ITEM_UPDATED = "item.updated"
ITEM_COMPLETED = "item.completed"
FILES_PERSISTED = "files.persisted"
RUNTIME_ERROR = "runtime.error"


class OpenCodeNormalizer:
    """Stateful OpenCode SSE stream normalizer for one backing execution."""

    def __init__(self, chat_id: str, execution_id: str) -> None:
        self._chat_id = chat_id
        self._execution_id = execution_id
        self._turn_id: str | None = None
        self._started_for_turn = False
        self._completed_for_turn = False
        self._part_type_by_id: dict[str, str] = {}
        self._part_message_id_by_id: dict[str, str] = {}
        self._message_role_by_id: dict[str, str] = {}
        self._parts_with_streamed_delta: set[str] = set()
        self._parts_with_snapshot_emitted: set[str] = set()
        self._started_tool_items: set[str] = set()
        self._completed_tool_items: set[str] = set()

    def reset(self) -> None:
        self._turn_id = None
        self._started_for_turn = False
        self._completed_for_turn = False
        self._part_type_by_id.clear()
        self._part_message_id_by_id.clear()
        self._message_role_by_id.clear()
        self._parts_with_streamed_delta.clear()
        self._parts_with_snapshot_emitted.clear()
        self._started_tool_items.clear()
        self._completed_tool_items.clear()

    def normalize(self, event: HarnessEvent) -> list[ChatEvent]:
        match event.event_type:
            case "session.status":
                return self._session_status(event)
            case "session.idle":
                return self._session_idle(event)
            case "session.error":
                return [
                    *self._ensure_turn_started(event),
                    self._runtime_error(event),
                    self._turn_completed_error(event),
                ]
            case value if is_turn_boundary_event(value):
                return self._synthetic_turn_completed(event)
            case "message.part.delta":
                return self._message_part_delta(event)
            case "message.part.updated":
                return self._message_part_updated(event)
            case "message.updated":
                return self._message_updated(event)
            case "agent_message_chunk":
                return [
                    *self._ensure_turn_started(event),
                    self._content_delta(event, "assistant_text"),
                ]
            case "agent_thought_chunk":
                return [
                    *self._ensure_turn_started(event),
                    self._content_delta(event, "reasoning_text"),
                ]
            case "tool_call":
                return [*self._ensure_turn_started(event), self._item_event(ITEM_STARTED, event)]
            case "tool_call_update":
                return [
                    *self._ensure_turn_started(event),
                    self._item_event(ITEM_UPDATED, event),
                    *self._file_events(event),
                ]
            case "files/persisted" | "files.persisted" | "file.write" | "file.persisted":
                return [*self._ensure_turn_started(event), *self._file_events(event)]
            case _:
                return []

    def _session_status(self, event: HarnessEvent) -> list[ChatEvent]:
        status = _extract_session_status(event.payload)
        if status != "busy":
            return []
        return self._ensure_turn_started(event)

    def _session_idle(self, event: HarnessEvent) -> list[ChatEvent]:
        if not self._started_for_turn:
            return []
        completed = self._turn_completed(event)
        if completed is None:
            return []
        return [completed]

    def _message_part_delta(self, event: HarnessEvent) -> list[ChatEvent]:
        properties = as_dict(event.payload.get("properties")) or event.payload
        part_id = as_str(properties.get("partID")) or as_str(properties.get("part_id"))
        if part_id is None:
            return []
        message_id = _extract_message_id(properties)
        if message_id is not None:
            self._part_message_id_by_id[part_id] = message_id
        stream_kind = self._stream_kind_for_part(part_id, message_id=message_id)
        if stream_kind is None:
            return []
        text = as_str(properties.get("delta"))
        if text is None and as_str(properties.get("field")) == "text":
            text = as_str(properties.get("text"))
        if text is None:
            return []
        self._parts_with_streamed_delta.add(part_id)
        return [
            *self._ensure_turn_started(event),
            self._event(
                CONTENT_DELTA,
                event,
                item_id=part_id,
                payload={"stream_kind": stream_kind, "text": text},
            ),
        ]

    def _message_part_updated(self, event: HarnessEvent) -> list[ChatEvent]:
        part = _extract_part(event.payload)
        if part is None:
            return []
        self._record_part_type(part)
        return self._events_from_part_snapshot(event, part)

    def _message_updated(self, event: HarnessEvent) -> list[ChatEvent]:
        self._record_message_role(event.payload)
        parts = _extract_message_parts(event.payload)
        if not parts:
            return []
        events: list[ChatEvent] = []
        for part in parts:
            self._record_part_type(part)
            events.extend(self._events_from_part_snapshot(event, part))
        return events

    def _record_part_type(self, part: dict[str, object]) -> None:
        part_id = as_str(part.get("id"))
        part_type = as_str(part.get("type"))
        if part_id is not None and part_type is not None:
            self._part_type_by_id[part_id] = part_type
            message_id = _extract_message_id(part)
            if message_id is not None:
                self._part_message_id_by_id[part_id] = message_id

    def _record_message_role(self, payload: dict[str, object]) -> None:
        message_id, role = _extract_message_identity(payload)
        if message_id is None or role is None:
            return
        self._message_role_by_id[message_id] = role.lower()

    def _events_from_part_snapshot(
        self, event: HarnessEvent, part: dict[str, object]
    ) -> list[ChatEvent]:
        part_id = as_str(part.get("id")) or f"item-{uuid4()}"
        part_type = as_str(part.get("type"))
        if part_type in {"text", "reasoning"} and not self._is_assistant_part(part_id, part):
            return []
        if part_type == "text":
            return self._text_snapshot_event(event, part, part_id, stream_kind="assistant_text")
        if part_type == "reasoning":
            return self._text_snapshot_event(event, part, part_id, stream_kind="reasoning_text")
        if part_type == "tool":
            return self._tool_part_events(event, part, part_id)
        return []

    def _text_snapshot_event(
        self,
        event: HarnessEvent,
        part: dict[str, object],
        part_id: str,
        *,
        stream_kind: str,
    ) -> list[ChatEvent]:
        text = as_str(part.get("text")) or ""
        if not text:
            return []
        if (
            part_id in self._parts_with_streamed_delta
            or part_id in self._parts_with_snapshot_emitted
        ):
            return []
        self._parts_with_snapshot_emitted.add(part_id)
        if stream_kind == "assistant_text":
            self._parts_with_streamed_delta.add(part_id)
        return [
            *self._ensure_turn_started(event),
            self._event(
                CONTENT_DELTA,
                event,
                item_id=part_id,
                payload={"stream_kind": stream_kind, "text": text},
            ),
        ]

    def _tool_part_events(
        self, event: HarnessEvent, part: dict[str, object], part_id: str
    ) -> list[ChatEvent]:
        state = as_dict(part.get("state")) or {}
        status = as_str(state.get("status"))
        if status is None:
            return []
        item_id = as_str(part.get("callID")) or part_id
        payload = _tool_payload(part)
        events = [*self._ensure_turn_started(event)]

        if status == "pending":
            if item_id in self._started_tool_items:
                return events
            self._started_tool_items.add(item_id)
            events.append(
                self._event(ITEM_STARTED, event, item_id=item_id, payload=dict(payload))
            )
            return events

        if status == "running":
            if item_id not in self._started_tool_items:
                self._started_tool_items.add(item_id)
                events.append(
                    self._event(ITEM_STARTED, event, item_id=item_id, payload=dict(payload))
                )
            events.append(self._event(ITEM_UPDATED, event, item_id=item_id, payload=dict(payload)))
            events.extend(self._file_events(event))
            return events

        if status == "completed":
            if item_id in self._completed_tool_items:
                return events
            if item_id not in self._started_tool_items:
                self._started_tool_items.add(item_id)
                events.append(
                    self._event(ITEM_STARTED, event, item_id=item_id, payload=dict(payload))
                )
            self._completed_tool_items.add(item_id)
            events.append(
                self._event(ITEM_COMPLETED, event, item_id=item_id, payload=dict(payload))
            )
            events.extend(self._file_events(event))
            return events

        return events

    def _stream_kind_for_part(self, part_id: str, *, message_id: str | None = None) -> str | None:
        if not self._is_assistant_part(part_id, message_id=message_id):
            return None
        part_type = self._part_type_by_id.get(part_id)
        if part_type == "text":
            return "assistant_text"
        if part_type == "reasoning":
            return "reasoning_text"
        return None

    def _is_assistant_part(
        self,
        part_id: str,
        part: dict[str, object] | None = None,
        *,
        message_id: str | None = None,
    ) -> bool:
        resolved_message_id = message_id or self._part_message_id_by_id.get(part_id)
        if resolved_message_id is None and part is not None:
            resolved_message_id = _extract_message_id(part)
            if resolved_message_id is not None:
                self._part_message_id_by_id[part_id] = resolved_message_id
        if resolved_message_id is None:
            return False
        role = self._message_role_by_id.get(resolved_message_id)
        return role == "assistant"

    def _ensure_turn_started(self, event: HarnessEvent) -> list[ChatEvent]:
        if self._started_for_turn:
            return []
        self._turn_id = _extract_turn_id(event.payload) or f"turn-{uuid4()}"
        self._started_for_turn = True
        self._completed_for_turn = False
        payload: dict[str, Any] = {}
        session_id = _extract_session_id(event.payload)
        if session_id is not None:
            payload["session_id"] = session_id
        if "model" in event.payload:
            payload["model"] = event.payload["model"]
        return [self._event(TURN_STARTED, event, payload=payload)]

    def _turn_completed(self, event: HarnessEvent) -> ChatEvent | None:
        if self._completed_for_turn:
            return None
        if self._turn_id is None:
            self._turn_id = _extract_turn_id(event.payload) or f"turn-{uuid4()}"
        payload: dict[str, Any] = {"status": "succeeded"}
        for key in ("usage", "duration_ms"):
            if key in event.payload:
                payload[key] = event.payload[key]
        info = as_dict(event.payload.get("info"))
        if "usage" not in payload and info is not None:
            usage = _usage_from_info(info)
            if usage:
                payload["usage"] = usage
        self._completed_for_turn = True
        chat_event = self._event(TURN_COMPLETED, event, payload=payload)
        self._reset_turn_state()
        return chat_event

    def _turn_completed_error(self, event: HarnessEvent) -> ChatEvent:
        if self._turn_id is None:
            self._turn_id = _extract_turn_id(event.payload) or f"turn-{uuid4()}"
        payload: dict[str, Any] = {"status": "error"}
        payload["error"] = event.payload.get("error") or event.payload.get("message") or "unknown"
        for key in ("usage", "duration_ms"):
            if key in event.payload:
                payload[key] = event.payload[key]
        self._completed_for_turn = True
        chat_event = self._event(TURN_COMPLETED, event, payload=payload)
        self._reset_turn_state()
        return chat_event

    def _synthetic_turn_completed(self, event: HarnessEvent) -> list[ChatEvent]:
        if not self._started_for_turn or self._completed_for_turn:
            return []
        completed = self._turn_completed(event)
        return [completed] if completed is not None else []

    def _runtime_error(self, event: HarnessEvent) -> ChatEvent:
        payload = dict(event.payload)
        payload.setdefault("supports_runtime_hitl", False)
        return self._event(RUNTIME_ERROR, event, payload=payload)

    def _content_delta(self, event: HarnessEvent, stream_kind: str) -> ChatEvent:
        return self._event(
            CONTENT_DELTA,
            event,
            item_id=as_str(event.payload.get("item_id")),
            payload={"stream_kind": stream_kind, "text": _text_from_payload(event.payload)},
        )

    def _item_event(self, event_type: str, event: HarnessEvent) -> ChatEvent:
        tool = _tool_payload_from_event(event.payload)
        item_id = (
            as_str(tool.get("id"))
            or as_str(event.payload.get("item_id"))
            or f"item-{uuid4()}"
        )
        raw_type = as_str(tool.get("type")) or as_str(event.payload.get("type"))
        name = as_str(tool.get("name")) or as_str(event.payload.get("name"))
        payload = dict(event.payload)
        payload["item_type"] = canonical_item_type(raw_type, name)
        if raw_type is not None:
            payload["raw_type"] = raw_type
        if name is not None:
            payload["name"] = name
        return self._event(event_type, event, item_id=item_id, payload=payload)

    def _file_events(self, event: HarnessEvent) -> list[ChatEvent]:
        files = extract_files(event.payload)
        if not files:
            return []
        return [self._event(FILES_PERSISTED, event, payload={"files": files})]

    def _reset_turn_state(self) -> None:
        self._turn_id = None
        self._started_for_turn = False
        self._part_type_by_id.clear()
        self._part_message_id_by_id.clear()
        self._message_role_by_id.clear()
        self._parts_with_streamed_delta.clear()
        self._parts_with_snapshot_emitted.clear()
        self._started_tool_items.clear()
        self._completed_tool_items.clear()

    def _event(
        self,
        event_type: str,
        event: HarnessEvent,
        *,
        item_id: str | None = None,
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
            payload=payload or {},
            harness_id=event.harness_id,
        )


def _extract_session_status(payload: dict[str, object]) -> str | None:
    properties = as_dict(payload.get("properties")) or payload
    status = as_dict(properties.get("status"))
    if status is not None:
        return as_str(status.get("type"))
    return as_str(properties.get("status"))


def _extract_part(payload: dict[str, object]) -> dict[str, object] | None:
    properties = as_dict(payload.get("properties")) or payload
    return as_dict(properties.get("part"))


def _extract_message_identity(payload: dict[str, object]) -> tuple[str | None, str | None]:
    properties = as_dict(payload.get("properties")) or payload
    info = as_dict(properties.get("info"))
    if info is None:
        info = as_dict(payload.get("info"))
    if info is not None:
        message_id = as_str(info.get("id"))
        role = as_str(info.get("role"))
        if message_id is not None or role is not None:
            return message_id, role

    message = as_dict(properties.get("message"))
    if message is None:
        message = as_dict(payload.get("message"))
    if message is not None:
        return _extract_message_id(message), as_str(message.get("role"))
    return None, None


def _extract_message_id(payload: dict[str, object]) -> str | None:
    if as_str(payload.get("messageID")) is not None:
        return as_str(payload.get("messageID"))
    if as_str(payload.get("messageId")) is not None:
        return as_str(payload.get("messageId"))
    if as_str(payload.get("message_id")) is not None:
        return as_str(payload.get("message_id"))
    if "role" in payload:
        return as_str(payload.get("id"))
    return None


def _extract_message_parts(payload: dict[str, object]) -> list[dict[str, object]]:
    properties = as_dict(payload.get("properties")) or payload
    message = as_dict(properties.get("message"))
    if message is None:
        message = as_dict(payload.get("message"))
    if message is None:
        return []
    parts = message.get("parts")
    if not isinstance(parts, list):
        return []
    result: list[dict[str, object]] = []
    for part in cast("list[object]", parts):
        if isinstance(part, dict):
            result.append(cast("dict[str, object]", part))
    return result


def _tool_payload(part: dict[str, object]) -> dict[str, object]:
    state = as_dict(part.get("state")) or {}
    raw_type = as_str(part.get("type"))
    name = as_str(part.get("tool"))
    payload: dict[str, object] = {
        "item_type": canonical_item_type(raw_type, name),
        "raw_type": raw_type or "tool",
        "name": name,
        "state": state,
    }
    for key in ("callID", "id", "tool"):
        if key in part:
            payload[key] = part[key]
    for key in ("input", "output", "metadata", "raw", "time"):
        if key in state:
            payload[key] = state[key]
    payload["status"] = state.get("status")
    return payload


def _extract_turn_id(payload: dict[str, object]) -> str | None:
    return as_str(payload.get("turn_id")) or as_str(payload.get("id"))


def _extract_session_id(payload: dict[str, object]) -> str | None:
    properties = as_dict(payload.get("properties")) or payload
    return (
        as_str(payload.get("session_id"))
        or as_str(payload.get("sessionId"))
        or as_str(properties.get("sessionID"))
        or as_str(properties.get("sessionId"))
    )


def _usage_from_info(info: dict[str, object]) -> dict[str, object]:
    usage: dict[str, object] = {}
    for source, target in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("cache_read_input_tokens", "cache_read_input_tokens"),
        ("cache_creation_input_tokens", "cache_creation_input_tokens"),
        ("reasoning_tokens", "reasoning_tokens"),
    ):
        if source in info:
            usage[target] = info[source]
    return usage


def _tool_payload_from_event(payload: dict[str, object]) -> dict[str, object]:
    value = payload.get("tool") or payload.get("tool_call")
    return cast("dict[str, object]", value) if isinstance(value, dict) else payload


def _text_from_payload(payload: dict[str, object]) -> str:
    for key in ("text", "delta", "content"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    properties = as_dict(payload.get("properties"))
    if properties is not None:
        return _text_from_payload(properties)
    return ""


__all__ = ["OpenCodeNormalizer"]
