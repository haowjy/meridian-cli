"""Claude HarnessEvent to ChatEvent normalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from meridian.lib.chat.normalization.common import (
    as_dict,
    as_str,
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

HARNESS_ID = "claude"
ITEM_STARTED = "item.started"
ITEM_UPDATED = "item.updated"
ITEM_COMPLETED = "item.completed"
FILES_PERSISTED = "files.persisted"


@dataclass
class _BlockState:
    index: int
    block_type: str
    item_id: str | None = None
    name: str | None = None
    input_json: str = ""


class ClaudeNormalizer:
    """Stateful Claude stream normalizer for one backing execution."""

    def __init__(self, chat_id: str, execution_id: str) -> None:
        self._chat_id = chat_id
        self._execution_id = execution_id
        self._turn_id: str | None = None
        self._blocks: dict[int, _BlockState] = {}
        self._completed_for_turn = False
        self._emitted_assistant_text = False
        self._aggregated_tools: dict[str, dict[str, Any]] = {}

    def reset(self) -> None:
        self._turn_id = None
        self._blocks.clear()
        self._completed_for_turn = False
        self._emitted_assistant_text = False
        self._aggregated_tools.clear()

    def normalize(self, event: HarnessEvent) -> list[ChatEvent]:
        match event.event_type:
            case "assistant":
                return self._assistant(event)
            case "user":
                return self._user(event)
            case "message_start":
                return [self._message_start(event)]
            case "content_block_start":
                return self._content_block_start(event)
            case "content_block_delta":
                return self._content_block_delta(event)
            case "content_block_stop":
                return self._content_block_stop(event)
            case "message_stop":
                return []
            case "result":
                return [
                    *self._file_events(event),
                    *self._result_content(event),
                    *self._turn_completed(event),
                ]
            case "files/persisted" | "files.persisted" | "file/write" | "file/persisted":
                return self._file_events(event) or [
                    self._event(FILES_PERSISTED, event, payload=dict(event.payload))
                ]
            case value if is_turn_boundary_event(value):
                return self._synthetic_turn_completed(event)
            case _:
                return []

    def _message_start(self, event: HarnessEvent) -> ChatEvent:
        self._turn_id = f"turn-{uuid4()}"
        self._blocks.clear()
        self._completed_for_turn = False
        self._emitted_assistant_text = False
        self._aggregated_tools.clear()
        payload = as_dict(event.payload.get("message")) or event.payload
        model = as_str(payload.get("model"))
        usage = as_dict(payload.get("usage"))
        event_payload: dict[str, Any] = {}
        if model is not None:
            event_payload["model"] = model
        if usage is not None:
            event_payload["usage"] = usage
        return self._event(TURN_STARTED, event, payload=event_payload)

    def _ensure_turn_started(self, event: HarnessEvent) -> list[ChatEvent]:
        if self._turn_id is not None:
            return []
        return [self._message_start(event)]

    def _assistant(self, event: HarnessEvent) -> list[ChatEvent]:
        events = [*self._ensure_turn_started(event)]
        for block in _assistant_blocks(event.payload):
            block_type = as_str(block.get("type"))
            if block_type in {"thinking", "thinking_delta"}:
                text = as_str(block.get("thinking")) or as_str(block.get("text")) or ""
                if text:
                    events.append(
                        self._event(
                            CONTENT_DELTA,
                            event,
                            payload={"stream_kind": "reasoning_text", "text": text},
                        )
                    )
                continue
            if block_type in {"text", "output_text", "text_delta"}:
                text = as_str(block.get("text")) or ""
                if text:
                    self._emitted_assistant_text = True
                    events.append(
                        self._event(
                            CONTENT_DELTA,
                            event,
                            payload={"stream_kind": "assistant_text", "text": text},
                        )
                    )
                continue
            if block_type != "tool_use":
                continue
            item_id = as_str(block.get("id")) or f"item-{uuid4()}"
            name = as_str(block.get("name"))
            input_payload = block.get("input")
            self._aggregated_tools[item_id] = {
                "name": name,
                "input": input_payload,
            }
            events.append(
                self._event(
                    ITEM_STARTED,
                    event,
                    item_id=item_id,
                    payload={
                        "item_type": name or "tool_use",
                        "name": name,
                        "raw_type": "tool_use",
                        "input": input_payload,
                    },
                )
            )
        return events

    def _user(self, event: HarnessEvent) -> list[ChatEvent]:
        tool_results = _user_tool_results(event.payload)
        if not tool_results:
            return []
        events = [*self._ensure_turn_started(event)]
        for result in tool_results:
            item_id = as_str(result.get("tool_use_id")) or f"item-{uuid4()}"
            known = self._aggregated_tools.get(item_id) or {}
            name = as_str(known.get("name"))
            payload: dict[str, Any] = {
                "item_type": name or "tool_use",
                "name": name,
                "raw_type": "tool_result",
                "content": result.get("content"),
            }
            if "is_error" in result:
                payload["is_error"] = result["is_error"]
            if result.get("is_error") is True:
                payload["error"] = result.get("content")
            if "input" in known:
                payload["input"] = known["input"]
            events.append(self._event(ITEM_COMPLETED, event, item_id=item_id, payload=payload))
            self._aggregated_tools.pop(item_id, None)
        return events

    def _result_content(self, event: HarnessEvent) -> list[ChatEvent]:
        if self._emitted_assistant_text:
            return []
        text = as_str(event.payload.get("result")) or as_str(event.payload.get("text")) or ""
        if not text:
            return []
        self._emitted_assistant_text = True
        return [
            *self._ensure_turn_started(event),
            self._event(
                CONTENT_DELTA,
                event,
                payload={"stream_kind": "assistant_text", "text": text},
            ),
        ]

    def _content_block_start(self, event: HarnessEvent) -> list[ChatEvent]:
        index = _index(event.payload)
        block = as_dict(event.payload.get("content_block")) or event.payload
        block_type = as_str(block.get("type")) or "unknown"
        item_id = as_str(block.get("id")) or (
            f"item-{uuid4()}" if block_type == "tool_use" else None
        )
        name = as_str(block.get("name"))
        self._blocks[index] = _BlockState(
            index=index,
            block_type=block_type,
            item_id=item_id,
            name=name,
        )
        if block_type != "tool_use" or item_id is None:
            return []
        return [
            self._event(
                ITEM_STARTED,
                event,
                item_id=item_id,
                payload={
                    "item_type": name or "tool_use",
                    "name": name,
                    "raw_type": block_type,
                },
            )
        ]

    def _content_block_delta(self, event: HarnessEvent) -> list[ChatEvent]:
        index = _index(event.payload)
        block = self._blocks.get(index)
        delta = as_dict(event.payload.get("delta")) or event.payload
        delta_type = as_str(delta.get("type"))
        if delta_type == "text_delta":
            text = as_str(delta.get("text")) or ""
            if text:
                self._emitted_assistant_text = True
            return [
                self._event(
                    CONTENT_DELTA,
                    event,
                    item_id=block.item_id if block else None,
                    payload={"stream_kind": "assistant_text", "text": text},
                )
            ]
        if delta_type == "thinking_delta":
            text = as_str(delta.get("thinking")) or as_str(delta.get("text")) or ""
            return [
                self._event(
                    CONTENT_DELTA,
                    event,
                    item_id=block.item_id if block else None,
                    payload={"stream_kind": "reasoning_text", "text": text},
                )
            ]
        if delta_type == "input_json_delta" and block is not None and block.item_id is not None:
            partial = as_str(delta.get("partial_json")) or ""
            block.input_json += partial
            return [
                self._event(
                    ITEM_UPDATED,
                    event,
                    item_id=block.item_id,
                    payload={"input_json_delta": partial, "input_json": block.input_json},
                )
            ]
        return []

    def _content_block_stop(self, event: HarnessEvent) -> list[ChatEvent]:
        index = _index(event.payload)
        block = self._blocks.pop(index, None)
        if block is None or block.block_type != "tool_use" or block.item_id is None:
            return []
        return [
            self._event(
                ITEM_COMPLETED,
                event,
                item_id=block.item_id,
                payload={
                    "item_type": block.name or "tool_use",
                    "name": block.name,
                    "input_json": block.input_json,
                },
            )
        ]

    def _turn_completed(self, event: HarnessEvent) -> list[ChatEvent]:
        if self._completed_for_turn:
            return []
        if self._turn_id is None:
            self._turn_id = f"turn-{uuid4()}"
        self._completed_for_turn = True
        payload: dict[str, Any] = {}
        for key in (
            "status",
            "exit_code",
            "error",
            "usage",
            "cost_usd",
            "duration_ms",
            "synthetic",
        ):
            if key in event.payload:
                payload[key] = event.payload[key]
        if "total_cost_usd" in event.payload:
            payload["cost_usd"] = event.payload["total_cost_usd"]
        chat_event = self._event(TURN_COMPLETED, event, payload=payload)
        self._turn_id = None
        self._blocks.clear()
        self._emitted_assistant_text = False
        self._aggregated_tools.clear()
        return [chat_event]

    def _synthetic_turn_completed(self, event: HarnessEvent) -> list[ChatEvent]:
        if self._turn_id is None or self._completed_for_turn:
            return []
        return self._turn_completed(event)

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


def _index(payload: dict[str, object]) -> int:
    value = payload.get("index")
    return value if isinstance(value, int) else 0


def _assistant_blocks(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, list):
        blocks: list[dict[str, object]] = []
        for entry in cast("list[object]", payload):
            blocks.extend(_assistant_blocks(entry))
        return blocks
    if isinstance(payload, dict):
        obj = cast("dict[str, object]", payload)
        block_type = as_str(obj.get("type"))
        if block_type in {
            "thinking",
            "thinking_delta",
            "text",
            "output_text",
            "text_delta",
            "tool_use",
            "tool_result",
        }:
            return [obj]
        blocks: list[dict[str, object]] = []
        for key in ("message", "content", "output"):
            if key in obj:
                blocks.extend(_assistant_blocks(obj[key]))
        return blocks
    return []


def _user_tool_results(payload: object) -> list[dict[str, object]]:
    blocks = _assistant_blocks(payload)
    return [block for block in blocks if as_str(block.get("type")) == "tool_result"]


__all__ = ["ClaudeNormalizer"]
