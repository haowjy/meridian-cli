"""Transcript providers and parsers for session-facing read paths."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import NamedTuple, Protocol, cast

from meridian.lib.harness.extractors.base import normalize_harness_event_type
from meridian.lib.launch.constants import HISTORY_FILENAME
from meridian.lib.state.history import iter_history_events

_TRANSCRIPT_TEXT_KEYS: tuple[str, ...] = (
    "text",
    "content",
    "message",
    "output",
    "toolUseResult",
)
_MAX_PREVIEW = 120


class TranscriptMessage(NamedTuple):
    role: str
    content: str


class TranscriptEventParser(Protocol):
    """Family parser for one transcript event dictionary."""

    def parse(self, event: dict[str, object]) -> tuple[list[TranscriptMessage], bool]:
        """Return extracted messages and compaction-boundary marker."""
        ...


class TranscriptProvider(Protocol):
    """Provider that loads structured event dictionaries from transcript files."""

    def supports(self, path: Path) -> bool: ...

    def iter_events(self, path: Path) -> Iterator[dict[str, object]]: ...


def text_from_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()

    if isinstance(value, list):
        payload = cast("list[object]", value)
        parts = [text_from_value(item) for item in payload]
        return "\n".join(part for part in parts if part).strip()

    if isinstance(value, dict):
        payload = cast("dict[str, object]", value)
        parts: list[str] = []
        for key in _TRANSCRIPT_TEXT_KEYS:
            if key not in payload:
                continue
            text = text_from_value(payload[key])
            if text:
                parts.append(text)
        return "\n".join(parts).strip()

    return ""


def _text_from_value(value: object) -> str:
    return text_from_value(value)


def _preview(value: str, *, limit: int = _MAX_PREVIEW) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3].rstrip()}..."


def _tool_use_summary(block: dict[str, object]) -> str:
    name = str(block.get("name", "tool")).strip() or "tool"
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return f"[tool: {name}]"

    input_payload = cast("dict[str, object]", tool_input)
    for key in ("file_path", "path", "command", "pattern", "description", "skill"):
        value = input_payload.get(key)
        if isinstance(value, str) and value.strip():
            return f"[tool: {name} {_preview(value.strip())}]"
    return f"[tool: {name}]"


def _tool_result_summary(block: dict[str, object]) -> str:
    content = text_from_value(block.get("content"))
    if not content:
        return "[tool_result]"
    return f"[tool_result] {content}"


def _normalize_message_text(value: str) -> str:
    return value.strip()


def _extract_claude_content(role: str, content: object) -> list[TranscriptMessage]:
    messages: list[TranscriptMessage] = []

    if isinstance(content, str):
        text = _normalize_message_text(content)
        if text:
            messages.append(TranscriptMessage(role=role, content=text))
        return messages

    if not isinstance(content, list):
        text = text_from_value(content)
        if text:
            messages.append(TranscriptMessage(role=role, content=text))
        return messages

    blocks = cast("list[object]", content)
    for item in blocks:
        if not isinstance(item, dict):
            text = text_from_value(item)
            if text:
                messages.append(TranscriptMessage(role=role, content=text))
            continue

        block = cast("dict[str, object]", item)
        block_type = str(block.get("type", "")).strip().lower()
        if block_type == "text":
            text = text_from_value(block.get("text"))
            if text:
                messages.append(TranscriptMessage(role=role, content=text))
            continue
        if role == "assistant" and block_type == "tool_use":
            messages.append(TranscriptMessage(role=role, content=_tool_use_summary(block)))
            continue
        if role == "user" and block_type == "tool_result":
            messages.append(TranscriptMessage(role=role, content=_tool_result_summary(block)))
            continue

        text = text_from_value(block)
        if text:
            messages.append(TranscriptMessage(role=role, content=text))

    return messages


def _extract_codex_response_item(payload: dict[str, object]) -> list[TranscriptMessage]:
    item_type = str(payload.get("type", "")).strip().lower()
    if item_type == "message":
        role = str(payload.get("role", "assistant")).strip().lower() or "assistant"
        content = payload.get("content")
        messages: list[TranscriptMessage] = []
        if isinstance(content, list):
            blocks = cast("list[object]", content)
            for block in blocks:
                if not isinstance(block, dict):
                    text = text_from_value(block)
                    if text:
                        messages.append(TranscriptMessage(role=role, content=text))
                    continue
                block_payload = cast("dict[str, object]", block)
                block_type = str(block_payload.get("type", "")).strip().lower()
                if block_type in {"input_text", "output_text", "text"}:
                    text = text_from_value(block_payload.get("text"))
                    if text:
                        messages.append(TranscriptMessage(role=role, content=text))
                    continue
                text = text_from_value(block_payload)
                if text:
                    messages.append(TranscriptMessage(role=role, content=text))
        else:
            text = text_from_value(content)
            if text:
                messages.append(TranscriptMessage(role=role, content=text))
        if not messages:
            fallback = text_from_value(payload.get("text"))
            if fallback:
                messages.append(TranscriptMessage(role=role, content=fallback))
        return messages

    if item_type == "function_call":
        name = str(payload.get("name", "tool")).strip() or "tool"
        arguments = text_from_value(payload.get("arguments"))
        rendered = f"[tool: {name}]"
        if arguments:
            rendered = f"[tool: {name} {_preview(arguments)}]"
        return [TranscriptMessage(role="assistant", content=rendered)]

    if item_type == "function_call_output":
        output = text_from_value(payload.get("output"))
        if output:
            return [TranscriptMessage(role="user", content=f"[tool_result] {output}")]
        return [TranscriptMessage(role="user", content="[tool_result]")]

    return []


def _extract_codex_exec_item(item: dict[str, object]) -> list[TranscriptMessage]:
    item_type = str(item.get("type", "")).strip().lower().replace("_", "").replace("-", "")
    if item_type == "agentmessage":
        text = text_from_value(item.get("text"))
        if not text:
            return []
        return [TranscriptMessage(role="assistant", content=text)]

    if item_type == "commandexecution":
        output = text_from_value(item.get("aggregated_output") or item.get("aggregatedOutput"))
        command = text_from_value(item.get("command"))
        if output:
            return [TranscriptMessage(role="user", content=f"[tool_result] {output}")]
        if command:
            return [
                TranscriptMessage(role="assistant", content=f"[tool: bash {_preview(command)}]")
            ]

    return []


class DefaultTranscriptEventParser(TranscriptEventParser):
    """Cross-harness event parser that normalizes Claude/Codex/OpenCode families."""

    def parse(self, event: dict[str, object]) -> tuple[list[TranscriptMessage], bool]:
        event_type = normalize_harness_event_type(event)

        if "event_type" in event and isinstance(event.get("payload"), dict):
            nested = dict(cast("dict[str, object]", event["payload"]))
            nested.setdefault("event_type", event["event_type"])
            return self.parse(nested)

        is_boundary = (
            event_type == "system"
            and str(event.get("subtype", "")).strip().lower() == "compact_boundary"
        )

        if event_type == "progress":
            data = event.get("data")
            if isinstance(data, dict):
                nested_message = cast("dict[str, object]", data).get("message")
                if isinstance(nested_message, dict):
                    nested_messages, nested_boundary = self.parse(
                        cast("dict[str, object]", nested_message)
                    )
                    return nested_messages, is_boundary or nested_boundary
            return ([], is_boundary)

        if event_type in {"assistant", "user"}:
            role = event_type
            message = event.get("message")
            if isinstance(message, dict):
                content = cast("dict[str, object]", message).get("content")
                extracted = _extract_claude_content(role, content)
                if extracted:
                    return extracted, is_boundary
            extracted = _extract_claude_content(role, event.get("content"))
            if extracted:
                return extracted, is_boundary
            raw_text = message if isinstance(message, str) else event.get("text")
            text = text_from_value(raw_text)
            if text:
                return ([TranscriptMessage(role=role, content=text)], is_boundary)
            fallback_text = text_from_value(event.get("tool_use_result"))
            if role == "user" and fallback_text:
                return (
                    [TranscriptMessage(role="user", content=f"[tool_result] {fallback_text}")],
                    is_boundary,
                )
            return ([], is_boundary)

        if event_type == "response_item":
            raw_payload = event.get("payload")
            if isinstance(raw_payload, dict):
                extracted = _extract_codex_response_item(cast("dict[str, object]", raw_payload))
                return (extracted, is_boundary)
            return ([], is_boundary)

        if event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict):
                return (_extract_codex_exec_item(cast("dict[str, object]", item)), is_boundary)
            return ([], is_boundary)

        role = str(event.get("role", "")).strip().lower()
        if role in {"assistant", "user", "system"}:
            text = text_from_value(event.get("content"))
            if text:
                return ([TranscriptMessage(role=role, content=text)], is_boundary)

        return ([], is_boundary)


class JsonlTranscriptProvider(TranscriptProvider):
    """Generic JSONL provider for harness-native transcript files."""

    def supports(self, path: Path) -> bool:
        return path.name != HISTORY_FILENAME

    def iter_events(self, path: Path) -> Iterator[dict[str, object]]:
        yield from _iter_json_events(path)


def _iter_json_events(path: Path) -> Iterator[dict[str, object]]:
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload_obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(payload_obj, dict):
                yield cast("dict[str, object]", payload_obj)
                continue
            if isinstance(payload_obj, list):
                for item in cast("list[object]", payload_obj):
                    if isinstance(item, dict):
                        yield cast("dict[str, object]", item)


def _opencode_db_path_for_session_file(path: Path) -> Path | None:
    if path.parent.name not in {"session_diff", "session"}:
        return None
    storage_root = path.parent.parent
    if storage_root.name != "storage":
        return None
    return storage_root.parent / "opencode.db"


def _load_opencode_db_events(path: Path) -> list[dict[str, object]]:
    session_id = path.stem.strip()
    if not session_id:
        return []

    db_path = _opencode_db_path_for_session_file(path)
    if db_path is None or not db_path.is_file():
        return []

    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.1) as connection:
            message_rows = connection.execute(
                """
                SELECT id, data, time_created
                FROM message
                WHERE session_id = ?
                ORDER BY time_created ASC, id ASC
                """,
                (session_id,),
            ).fetchall()
            part_rows = connection.execute(
                """
                SELECT message_id, data, time_created, id
                FROM part
                WHERE session_id = ?
                ORDER BY time_created ASC, id ASC
                """,
                (session_id,),
            ).fetchall()
    except (OSError, sqlite3.Error):
        return []

    if not message_rows:
        return []

    parts_by_message: dict[str, list[dict[str, object]]] = defaultdict(list)
    for message_id, part_data, _time_created, _part_id in part_rows:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            continue
        try:
            part_obj = json.loads(str(part_data))
        except (TypeError, ValueError):
            continue
        if isinstance(part_obj, dict):
            parts_by_message[normalized_message_id].append(cast("dict[str, object]", part_obj))

    events: list[dict[str, object]] = []
    for message_id, message_data, _time_created in message_rows:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            continue
        try:
            message_obj = json.loads(str(message_data))
        except (TypeError, ValueError):
            continue
        if not isinstance(message_obj, dict):
            continue

        message_payload = cast("dict[str, object]", message_obj)
        role = str(message_payload.get("role", "")).strip().lower()
        if role not in {"assistant", "user", "system"}:
            continue

        text_parts: list[str] = []
        for part in parts_by_message.get(normalized_message_id, []):
            if str(part.get("type", "")).strip().lower() != "text":
                continue
            text = text_from_value(part.get("text"))
            if text:
                text_parts.append(text)

        content = "\n".join(text_parts).strip()
        if not content:
            content = text_from_value(message_payload.get("content"))
        if not content:
            content = text_from_value(message_payload.get("text"))
        if not content:
            continue
        events.append({"role": role, "content": content})

    return events


class OpenCodeStorageTranscriptProvider(TranscriptProvider):
    """OpenCode storage provider that prefers opencode.db transcript rows."""

    def supports(self, path: Path) -> bool:
        return (
            path.suffix == ".json"
            and path.parent.name in {"session_diff", "session"}
            and path.parent.parent.name == "storage"
        )

    def iter_events(self, path: Path) -> Iterator[dict[str, object]]:
        db_events = _load_opencode_db_events(path)
        if db_events:
            yield from db_events
            return
        yield from _iter_json_events(path)


class HistoryJsonlTranscriptProvider(TranscriptProvider):
    """History-provider using crash-tolerant history iterators for canonicalized paths."""

    def supports(self, path: Path) -> bool:
        return path.name == HISTORY_FILENAME

    def iter_events(self, path: Path) -> Iterator[dict[str, object]]:
        for event in iter_history_events(path):
            yield cast("dict[str, object]", event)


_TRANSCRIPT_PROVIDERS: tuple[TranscriptProvider, ...] = (
    HistoryJsonlTranscriptProvider(),
    OpenCodeStorageTranscriptProvider(),
    JsonlTranscriptProvider(),
)


def _provider_for_path(path: Path) -> TranscriptProvider:
    for provider in _TRANSCRIPT_PROVIDERS:
        if provider.supports(path):
            return provider
    return JsonlTranscriptProvider()


def parse_transcript_events(
    events: Sequence[dict[str, object]],
    *,
    parser: TranscriptEventParser | None = None,
) -> tuple[list[list[TranscriptMessage]], int]:
    resolved_parser = parser or DefaultTranscriptEventParser()
    segments: list[list[TranscriptMessage]] = [[]]
    total_compactions = 0
    for event in events:
        extracted, boundary = resolved_parser.parse(event)
        if boundary:
            total_compactions += 1
            segments.append([])
            continue
        if extracted:
            segments[-1].extend(extracted)
    return segments, total_compactions


def iter_transcript_events(path: Path) -> Iterator[dict[str, object]]:
    provider = _provider_for_path(path)
    yield from provider.iter_events(path)


def parse_transcript_file(
    path: Path,
    *,
    parser: TranscriptEventParser | None = None,
) -> tuple[list[list[TranscriptMessage]], int]:
    resolved_parser = parser or DefaultTranscriptEventParser()
    segments: list[list[TranscriptMessage]] = [[]]
    total_compactions = 0
    for event in iter_transcript_events(path):
        extracted, boundary = resolved_parser.parse(event)
        if boundary:
            total_compactions += 1
            segments.append([])
            continue
        if extracted:
            segments[-1].extend(extracted)
    return segments, total_compactions


__all__ = [
    "DefaultTranscriptEventParser",
    "HistoryJsonlTranscriptProvider",
    "JsonlTranscriptProvider",
    "OpenCodeStorageTranscriptProvider",
    "TranscriptEventParser",
    "TranscriptMessage",
    "TranscriptProvider",
    "_text_from_value",
    "iter_transcript_events",
    "parse_transcript_events",
    "parse_transcript_file",
    "text_from_value",
]
