"""OpenCode HarnessEvent to ChatEvent normalization."""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

from meridian.lib.chat.normalization.common import canonical_item_type
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
FILES_PERSISTED = "files.persisted"
RUNTIME_ERROR = "runtime.error"


class OpenCodeNormalizer:
    """Stateful OpenCode SSE stream normalizer for one backing execution."""

    def __init__(self, chat_id: str, execution_id: str) -> None:
        self._chat_id = chat_id
        self._execution_id = execution_id
        self._turn_id: str | None = None
        self._started_for_turn = False

    def reset(self) -> None:
        self._turn_id = None
        self._started_for_turn = False

    def normalize(self, event: HarnessEvent) -> list[ChatEvent]:
        match event.event_type:
            case "session.idle":
                return [*self._ensure_turn_started(event), self._turn_completed(event)]
            case "session.error":
                return [
                    *self._ensure_turn_started(event),
                    self._runtime_error(event),
                    self._turn_completed_error(event),
                ]
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

    def _ensure_turn_started(self, event: HarnessEvent) -> list[ChatEvent]:
        if self._started_for_turn:
            return []
        self._turn_id = (
            _str(event.payload.get("turn_id"))
            or _str(event.payload.get("id"))
            or f"turn-{uuid4()}"
        )
        self._started_for_turn = True
        payload: dict[str, Any] = {}
        for key in ("model", "session_id"):
            if key in event.payload:
                payload[key] = event.payload[key]
        return [self._event(TURN_STARTED, event, payload=payload)]

    def _turn_completed(self, event: HarnessEvent) -> ChatEvent:
        if self._turn_id is None:
            self._turn_id = _str(event.payload.get("turn_id")) or f"turn-{uuid4()}"
        payload: dict[str, Any] = {"status": "succeeded"}
        for key in ("usage", "duration_ms"):
            if key in event.payload:
                payload[key] = event.payload[key]
        chat_event = self._event(TURN_COMPLETED, event, payload=payload)
        self._turn_id = None
        self._started_for_turn = False
        return chat_event

    def _turn_completed_error(self, event: HarnessEvent) -> ChatEvent:
        if self._turn_id is None:
            self._turn_id = _str(event.payload.get("turn_id")) or f"turn-{uuid4()}"
        payload: dict[str, Any] = {"status": "error"}
        payload["error"] = event.payload.get("error") or event.payload.get("message") or "unknown"
        for key in ("usage", "duration_ms"):
            if key in event.payload:
                payload[key] = event.payload[key]
        chat_event = self._event(TURN_COMPLETED, event, payload=payload)
        self._turn_id = None
        self._started_for_turn = False
        return chat_event

    def _runtime_error(self, event: HarnessEvent) -> ChatEvent:
        payload = dict(event.payload)
        payload.setdefault("supports_runtime_hitl", False)
        return self._event(RUNTIME_ERROR, event, payload=payload)

    def _content_delta(self, event: HarnessEvent, stream_kind: str) -> ChatEvent:
        return self._event(
            CONTENT_DELTA,
            event,
            item_id=_str(event.payload.get("item_id")),
            payload={"stream_kind": stream_kind, "text": _text_from_payload(event.payload)},
        )

    def _item_event(self, event_type: str, event: HarnessEvent) -> ChatEvent:
        tool = _tool_payload(event.payload)
        item_id = _str(tool.get("id")) or _str(event.payload.get("item_id")) or f"item-{uuid4()}"
        raw_type = _str(tool.get("type")) or _str(event.payload.get("type"))
        name = _str(tool.get("name")) or _str(event.payload.get("name"))
        payload = dict(event.payload)
        payload["item_type"] = canonical_item_type(raw_type, name)
        if raw_type is not None:
            payload["raw_type"] = raw_type
        if name is not None:
            payload["name"] = name
        return self._event(event_type, event, item_id=item_id, payload=payload)

    def _file_events(self, event: HarnessEvent) -> list[ChatEvent]:
        files = _extract_files(event.payload)
        if not files:
            return []
        return [self._event(FILES_PERSISTED, event, payload={"files": files})]

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


def _tool_payload(payload: dict[str, object]) -> dict[str, object]:
    value = payload.get("tool") or payload.get("tool_call")
    return cast("dict[str, object]", value) if isinstance(value, dict) else payload


def _text_from_payload(payload: dict[str, object]) -> str:
    for key in ("text", "delta", "content"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    properties = payload.get("properties")
    if isinstance(properties, dict):
        return _text_from_payload(cast("dict[str, object]", properties))
    return ""


def _extract_files(payload: dict[str, object]) -> list[dict[str, object]]:
    value = payload.get("files") or payload.get("paths")
    if isinstance(value, list):
        files: list[dict[str, object]] = []
        for entry in cast("list[object]", value):
            if isinstance(entry, str):
                files.append({"path": entry})
            elif isinstance(entry, dict):
                files.append(cast("dict[str, object]", entry))
        return files
    path = payload.get("path") or payload.get("file")
    if isinstance(path, str):
        result: dict[str, object] = {"path": path}
        for key in ("operation", "status"):
            if key in payload:
                result[key] = payload[key]
        return [result]
    properties = payload.get("properties")
    if isinstance(properties, dict):
        return _extract_files(cast("dict[str, object]", properties))
    return []


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


__all__ = ["OpenCodeNormalizer"]
