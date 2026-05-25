"""Transcript providers and parsers for session-facing read paths."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import NamedTuple, Protocol, cast

from meridian.lib.harness.extractors.base import normalize_harness_event_type
from meridian.lib.harness.opencode_transcript import OpenCodeStorageTranscriptProvider
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


class TranscriptParseResult(NamedTuple):
    segments: list[list[TranscriptMessage]]
    total_compactions: int
    segment_setups: tuple[str | None, ...]
    consumed_setup_event_indexes: tuple[int, ...] = ()

    @property
    def segment_prologues(self) -> tuple[str | None, ...]:
        """Backward-compatible alias for session setup slots."""
        return self.segment_setups


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


class HistoryJsonlTranscriptProvider(TranscriptProvider):
    """History-provider using crash-tolerant history iterators for canonicalized paths."""

    def supports(self, path: Path) -> bool:
        return path.name == HISTORY_FILENAME

    def iter_events(self, path: Path) -> Iterator[dict[str, object]]:
        for event in iter_history_events(path):
            yield cast("dict[str, object]", event)


_TRANSCRIPT_PROVIDERS: tuple[TranscriptProvider, ...] = (
    HistoryJsonlTranscriptProvider(),
    OpenCodeStorageTranscriptProvider(
        text_from_value=text_from_value,
        iter_json_events=_iter_json_events,
    ),
    JsonlTranscriptProvider(),
)


def _provider_for_path(path: Path) -> TranscriptProvider:
    for provider in _TRANSCRIPT_PROVIDERS:
        if provider.supports(path):
            return provider
    return JsonlTranscriptProvider()


def _unwrap_seq_envelope(event: dict[str, object]) -> dict[str, object]:
    if "event_type" in event and isinstance(event.get("payload"), dict):
        nested = dict(cast("dict[str, object]", event["payload"]))
        nested.setdefault("event_type", event["event_type"])
        return _unwrap_seq_envelope(nested)
    return event


def _join_message_content(messages: list[TranscriptMessage]) -> str | None:
    parts = [message.content.strip() for message in messages if message.content.strip()]
    if not parts:
        return None
    return "\n\n".join(parts).strip()


def _is_claude_compaction_boundary(event: dict[str, object]) -> bool:
    event_type = normalize_harness_event_type(event)
    subtype = str(event.get("subtype", "")).strip().lower()
    return event_type == "system" and subtype == "compact_boundary"


def _is_opencode_compaction_boundary(event: dict[str, object]) -> bool:
    part = event.get("part")
    if not isinstance(part, dict):
        return False
    part_payload = cast("dict[str, object]", part)
    return str(part_payload.get("type", "")).strip().lower() == "compaction"


def _extract_claude_system_prologue(event: dict[str, object]) -> str | None:
    event_type = normalize_harness_event_type(event)
    if event_type != "system":
        return None
    if _is_claude_compaction_boundary(event):
        return None
    return text_from_value(event.get("content")) or text_from_value(event.get("text")) or None


def _extract_claude_boundary_handoff(event: dict[str, object]) -> str | None:
    if not _is_claude_compaction_boundary(event):
        return None
    return (
        text_from_value(event.get("summary"))
        or text_from_value(event.get("handoff"))
        or text_from_value(event.get("content"))
        or None
    )


def _extract_claude_follow_on_handoff(
    event: dict[str, object],
    extracted_messages: list[TranscriptMessage],
) -> str | None:
    if normalize_harness_event_type(event) != "user":
        return None
    if not bool(event.get("isSynthetic")):
        return None
    return _join_message_content(extracted_messages)


def _extract_opencode_follow_on_handoff(
    event: dict[str, object],
    extracted_messages: list[TranscriptMessage],
) -> str | None:
    role = str(event.get("role", "")).strip().lower()
    mode = str(event.get("mode", "")).strip().lower()
    agent = str(event.get("agent", "")).strip().lower()
    if role != "assistant" or mode != "compaction" or agent != "compaction":
        return None

    part_texts: list[str] = []
    for key in ("part", "parts"):
        value = event.get(key)
        if isinstance(value, dict):
            part_payload = cast("dict[str, object]", value)
            if str(part_payload.get("type", "")).strip().lower() == "text":
                text = text_from_value(part_payload.get("text"))
                if text:
                    part_texts.append(text)
        elif isinstance(value, list):
            for item in cast("list[object]", value):
                if not isinstance(item, dict):
                    continue
                part_payload = cast("dict[str, object]", item)
                if str(part_payload.get("type", "")).strip().lower() != "text":
                    continue
                text = text_from_value(part_payload.get("text"))
                if text:
                    part_texts.append(text)

    if part_texts:
        return "\n".join(part_texts).strip()
    return _join_message_content(extracted_messages)


def _parse_events_with_prologues(
    events: Iterable[dict[str, object]],
    *,
    parser: TranscriptEventParser,
) -> TranscriptParseResult:
    segments: list[list[TranscriptMessage]] = [[]]
    segment_setups: list[str | None] = [None]
    total_compactions = 0
    consumed_setup_event_indexes: list[int] = []
    pending_follow_on_summary: tuple[int, str] | None = None

    for event_index, event in enumerate(events):
        normalized_event = _unwrap_seq_envelope(event)
        extracted_messages, parser_boundary = parser.parse(event)

        is_opencode_boundary = _is_opencode_compaction_boundary(normalized_event)
        is_claude_boundary = _is_claude_compaction_boundary(normalized_event)
        boundary = parser_boundary or is_opencode_boundary

        if boundary:
            total_compactions += 1
            segments.append([])
            handoff = (
                _extract_claude_boundary_handoff(normalized_event)
                if is_claude_boundary
                else None
            )
            segment_setups.append(handoff)
            next_segment_index = len(segments) - 1
            if handoff is None:
                if is_claude_boundary:
                    pending_follow_on_summary = (next_segment_index, "claude")
                elif is_opencode_boundary:
                    pending_follow_on_summary = (next_segment_index, "opencode")
                else:
                    pending_follow_on_summary = None
            else:
                pending_follow_on_summary = None
            continue

        if pending_follow_on_summary is not None:
            segment_index, source = pending_follow_on_summary
            setup_text: str | None = None
            if source == "claude":
                setup_text = _extract_claude_follow_on_handoff(
                    normalized_event, extracted_messages
                )
            elif source == "opencode":
                setup_text = _extract_opencode_follow_on_handoff(
                    normalized_event, extracted_messages
                )
            pending_follow_on_summary = None
            if setup_text:
                segment_setups[segment_index] = setup_text
                consumed_setup_event_indexes.append(event_index)
                continue

        if segment_setups[-1] is None:
            prologue = _extract_claude_system_prologue(normalized_event)
            if prologue:
                segment_setups[-1] = prologue

        if extracted_messages:
            segments[-1].extend(extracted_messages)

    return TranscriptParseResult(
        segments=segments,
        total_compactions=total_compactions,
        segment_setups=tuple(segment_setups),
        consumed_setup_event_indexes=tuple(consumed_setup_event_indexes),
    )


def parse_transcript_events(
    events: Sequence[dict[str, object]],
    *,
    parser: TranscriptEventParser | None = None,
) -> tuple[list[list[TranscriptMessage]], int]:
    parsed = parse_transcript_events_with_prologues(events, parser=parser)
    return parsed.segments, parsed.total_compactions


def parse_transcript_events_with_prologues(
    events: Sequence[dict[str, object]],
    *,
    parser: TranscriptEventParser | None = None,
) -> TranscriptParseResult:
    resolved_parser = parser or DefaultTranscriptEventParser()
    return _parse_events_with_prologues(events, parser=resolved_parser)


def iter_transcript_events(path: Path) -> Iterator[dict[str, object]]:
    provider = _provider_for_path(path)
    yield from provider.iter_events(path)


def parse_transcript_file(
    path: Path,
    *,
    parser: TranscriptEventParser | None = None,
) -> tuple[list[list[TranscriptMessage]], int]:
    parsed = parse_transcript_file_with_prologues(path, parser=parser)
    return parsed.segments, parsed.total_compactions


def parse_transcript_file_with_prologues(
    path: Path,
    *,
    parser: TranscriptEventParser | None = None,
) -> TranscriptParseResult:
    resolved_parser = parser or DefaultTranscriptEventParser()
    return _parse_events_with_prologues(iter_transcript_events(path), parser=resolved_parser)


__all__ = [
    "DefaultTranscriptEventParser",
    "HistoryJsonlTranscriptProvider",
    "JsonlTranscriptProvider",
    "OpenCodeStorageTranscriptProvider",
    "TranscriptEventParser",
    "TranscriptMessage",
    "TranscriptParseResult",
    "TranscriptProvider",
    "_text_from_value",
    "iter_transcript_events",
    "parse_transcript_events",
    "parse_transcript_events_with_prologues",
    "parse_transcript_file",
    "parse_transcript_file_with_prologues",
    "text_from_value",
]
