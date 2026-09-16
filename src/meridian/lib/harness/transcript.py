"""Transcript providers and parsers for session-facing read paths."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple, Protocol, cast

from meridian.lib.harness.extractors.base import normalize_harness_event_type
from meridian.lib.harness.opencode_transcript import (
    OpenCodeStorageTranscriptProvider,
    interpret_opencode_record,
    iter_opencode_db_events,
)
from meridian.lib.launch.constants import HISTORY_FILENAME
from meridian.lib.state.history import iter_history_events
from meridian.lib.state.native_snapshot import (
    HEADER_LIMIT,
    NATIVE_SNAPSHOT_FILENAME,
    SnapshotHeader,
    TranscriptReadPaused,
    TranscriptValidation,
    is_snapshot_prefix,
    read_jsonl_frame,
    read_snapshot,
    reject_unframed_storage_frame,
    reject_unframed_storage_record,
)

_TRANSCRIPT_TEXT_KEYS: tuple[str, ...] = (
    "text",
    "content",
    "message",
    "output",
    "toolUseResult",
)
_MAX_PREVIEW = 120


class ToolCall(NamedTuple):
    """Normalized tool invocation — harness-agnostic."""

    name: str  # Canonical lowercase name: bash, read, write, edit, grep, stdin, ...
    body: str  # Extracted meaningful payload: command string, file path, pattern, etc.


class TranscriptMessage(NamedTuple):
    role: str
    content: str
    tool_call: ToolCall | None = None
    is_tool_result: bool = False
    kind: Literal["interaction", "annotation"] = "interaction"


class TranscriptParseResult(NamedTuple):
    segments: list[list[TranscriptMessage]]
    total_compactions: int
    segment_setups: tuple[str | None, ...]
    consumed_setup_event_indexes: tuple[int, ...] = ()
    rendering_reason: str | None = None

    @property
    def segment_prologues(self) -> tuple[str | None, ...]:
        """Backward-compatible alias for session setup slots."""
        return self.segment_setups


class NormalizedTranscriptEvent(NamedTuple):
    messages: list[TranscriptMessage]
    boundary: bool = False
    consumed_setup: bool = False
    rendering_reason: str | None = None


class TranscriptEventParser(Protocol):
    """Family parser for one transcript event dictionary."""

    def parse(self, event: dict[str, object]) -> NormalizedTranscriptEvent:
        """Return extracted messages, boundaries and interpretation limits."""
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


# Harness tool names that map to shell execution.
_EXEC_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "exec_command",
        "shell",
        "terminal",
        "run_command",
    }
)

# Harness tool names for stdin interaction.
_STDIN_TOOL_NAMES: frozenset[str] = frozenset({"write_stdin"})

# Keys that carry the "interesting" payload in a Claude-style tool input dict.
_TOOL_BODY_KEYS: tuple[str, ...] = (
    "file_path",
    "path",
    "command",
    "pattern",
    "description",
    "skill",
)


def _normalize_tool(name: str, body: str) -> ToolCall:
    """Normalize a raw tool name + body into a canonical ToolCall."""
    lowered = name.strip().lower()

    # Shell-execution equivalents → bash
    if lowered == "bash":
        return ToolCall(name="bash", body=body)
    if lowered in _EXEC_TOOL_NAMES:
        # Codex JSON body: {"cmd":"..."} → extract cmd
        extracted = _extract_json_field(body, "cmd")
        return ToolCall(name="bash", body=extracted or body)

    # Stdin interaction → stdin
    if lowered in _STDIN_TOOL_NAMES:
        return ToolCall(name="stdin", body="")

    # Standard file/search tools → lowercase canonical
    for verb in ("read", "write", "edit", "grep"):
        if lowered == verb:
            return ToolCall(name=verb, body=body)

    return ToolCall(name=lowered or "tool", body=body)


def _extract_json_field(body: str, field: str) -> str | None:
    """Try to extract a string field from a JSON body."""
    try:
        parsed: object = json.loads(body)
        if isinstance(parsed, dict):
            value: object = cast("dict[str, object]", parsed).get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return None


def _tool_use_summary(block: dict[str, object]) -> tuple[str, ToolCall]:
    """Return (text_marker, normalized_tool_call) for a Claude tool_use block."""
    name = str(block.get("name", "tool")).strip() or "tool"
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return f"[tool: {name}]", _normalize_tool(name, "")

    input_payload = cast("dict[str, object]", tool_input)
    for key in _TOOL_BODY_KEYS:
        value = input_payload.get(key)
        if isinstance(value, str) and value.strip():
            body = value.strip()
            return f"[tool: {name} {_preview(body)}]", _normalize_tool(name, body)
    return f"[tool: {name}]", _normalize_tool(name, "")


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
            marker, tool_call = _tool_use_summary(block)
            messages.append(
                TranscriptMessage(
                    role=role,
                    content=marker,
                    tool_call=tool_call,
                )
            )
            continue
        if role == "assistant" and block_type in {"toolcall", "function_call", "functioncall"}:
            marker, tool_call = _pi_tool_call_summary(block)
            messages.append(
                TranscriptMessage(
                    role=role,
                    content=marker,
                    tool_call=tool_call,
                )
            )
            continue
        if role == "user" and block_type == "tool_result":
            messages.append(
                TranscriptMessage(
                    role=role,
                    content=_tool_result_summary(block),
                    is_tool_result=True,
                )
            )
            continue

        text = text_from_value(block)
        if text:
            messages.append(TranscriptMessage(role=role, content=text))

    return messages


def _json_preview_payload(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return text_from_value(value)


def _pi_tool_call_summary(block: dict[str, object]) -> tuple[str, ToolCall]:
    name = str(block.get("name", "tool")).strip() or "tool"
    body = _json_preview_payload(block.get("arguments"))
    rendered = f"[tool: {name}]"
    if body:
        rendered = f"[tool: {name} {_preview(body)}]"
    return rendered, _normalize_tool(name, body)


def _extract_pi_message_event(payload: dict[str, object]) -> NormalizedTranscriptEvent:
    """Interpret supported Pi material and diagnose omissions in the same pass."""
    raw_message = payload.get("message")
    if not isinstance(raw_message, dict):
        return NormalizedTranscriptEvent(
            [], rendering_reason="Malformed Pi message; rendering is incomplete."
        )
    message = cast("dict[str, object]", raw_message)
    role = str(message.get("role", "")).strip().lower()
    reason = "Malformed Pi message content; rendering is incomplete."
    if role == "bashexecution":
        command, output = message.get("command"), message.get("output")
        if not isinstance(command, str) or not isinstance(output, str):
            return NormalizedTranscriptEvent([], rendering_reason=reason)
        return NormalizedTranscriptEvent(
            [
                TranscriptMessage("user", f"[tool: bash {command}]", ToolCall("bash", command)),
                TranscriptMessage("user", f"[tool_result] {output}", is_tool_result=True),
            ]
        )
    if role not in {"assistant", "user", "system", "custom", "toolresult", "tool_result"}:
        return NormalizedTranscriptEvent(
            [], rendering_reason="Unsupported Pi message role; rendering is incomplete."
        )
    content = message.get("content")
    if not isinstance(content, (str, list)):
        return NormalizedTranscriptEvent([], rendering_reason=reason)
    blocks: list[object] = (
        [{"type": "text", "text": content}]
        if isinstance(content, str)
        else cast("list[object]", content)
    )
    messages: list[TranscriptMessage] = []
    rendering_reason: str | None = None
    rendered_role = "user" if role in {"custom", "toolresult", "tool_result"} else role
    for item in blocks:
        if not isinstance(item, dict):
            rendering_reason = reason
            continue
        block = cast("dict[str, object]", item)
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                rendering_reason = reason
            elif text.strip():
                messages.append(TranscriptMessage(rendered_role, text.strip()))
        elif block_type == "thinking" and role == "assistant":
            if not isinstance(block.get("thinking"), str):
                rendering_reason = reason
        elif block_type == "toolCall" and role == "assistant":
            name = block.get("name")
            if (
                not isinstance(name, str)
                or not name.strip()
                or not isinstance(block.get("arguments"), dict)
            ):
                rendering_reason = reason
                continue
            marker, tool = _pi_tool_call_summary(block)
            messages.append(TranscriptMessage("assistant", marker, tool))
        else:
            rendering_reason = "Unsupported Pi message content; rendering is incomplete."
    if role in {"toolresult", "tool_result"} and (messages or rendering_reason is None):
        text = "\n".join(message.content for message in messages)
        messages = [TranscriptMessage("user", f"[tool_result] {text}".strip(), is_tool_result=True)]
    return NormalizedTranscriptEvent(messages, rendering_reason=rendering_reason)


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
        tool_call = _normalize_tool(name, arguments)
        rendered = f"[tool: {name}]"
        if arguments:
            rendered = f"[tool: {name} {_preview(arguments)}]"
        return [TranscriptMessage(role="assistant", content=rendered, tool_call=tool_call)]

    if item_type == "function_call_output":
        output = text_from_value(payload.get("output"))
        if output:
            return [
                TranscriptMessage(
                    role="user",
                    content=f"[tool_result] {output}",
                    is_tool_result=True,
                )
            ]
        return [
            TranscriptMessage(
                role="user",
                content="[tool_result]",
                is_tool_result=True,
            )
        ]

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
            return [
                TranscriptMessage(
                    role="user",
                    content=f"[tool_result] {output}",
                    is_tool_result=True,
                )
            ]
        if command:
            tool_call = ToolCall(name="bash", body=command)
            return [
                TranscriptMessage(
                    role="assistant",
                    content=f"[tool: bash {_preview(command)}]",
                    tool_call=tool_call,
                )
            ]

    return []


class DefaultTranscriptEventParser(TranscriptEventParser):
    """Cross-harness event parser that normalizes Claude/Codex/OpenCode families."""

    def parse(self, event: dict[str, object]) -> NormalizedTranscriptEvent:
        event = _unwrap_seq_envelope(event)
        event_type = normalize_harness_event_type(event)

        is_boundary = (
            event_type == "system"
            and str(event.get("subtype", "")).strip().lower() == "compact_boundary"
        )

        if event_type == "progress":
            data = event.get("data")
            if isinstance(data, dict):
                nested_message = cast("dict[str, object]", data).get("message")
                if isinstance(nested_message, dict):
                    nested = self.parse(cast("dict[str, object]", nested_message))
                    return nested._replace(boundary=is_boundary or nested.boundary)
            return NormalizedTranscriptEvent([], is_boundary)

        if event_type in {"assistant", "user"}:
            role = event_type
            message = event.get("message")
            if isinstance(message, dict):
                content = cast("dict[str, object]", message).get("content")
                extracted = _extract_claude_content(role, content)
                if extracted:
                    return NormalizedTranscriptEvent(extracted, is_boundary)
            extracted = _extract_claude_content(role, event.get("content"))
            if extracted:
                return NormalizedTranscriptEvent(extracted, is_boundary)
            raw_text = message if isinstance(message, str) else event.get("text")
            text = text_from_value(raw_text)
            if text:
                return NormalizedTranscriptEvent(
                    [TranscriptMessage(role=role, content=text)], is_boundary
                )
            fallback_text = text_from_value(event.get("tool_use_result"))
            if role == "user" and fallback_text:
                return NormalizedTranscriptEvent(
                    [
                        TranscriptMessage(
                            role="user",
                            content=f"[tool_result] {fallback_text}",
                            is_tool_result=True,
                        )
                    ],
                    is_boundary,
                )
            return NormalizedTranscriptEvent([], is_boundary)

        if event_type == "response_item":
            raw_payload = event.get("payload")
            if isinstance(raw_payload, dict):
                extracted = _extract_codex_response_item(cast("dict[str, object]", raw_payload))
                return NormalizedTranscriptEvent(extracted, is_boundary)
            extracted = _extract_codex_response_item(event)
            return NormalizedTranscriptEvent(extracted, is_boundary)

        if event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict):
                return NormalizedTranscriptEvent(
                    _extract_codex_exec_item(cast("dict[str, object]", item)), is_boundary
                )
            return NormalizedTranscriptEvent([], is_boundary)

        if event_type == "message_end" or (event_type == "message" and "message" in event):
            return _extract_pi_message_event(event)
        if event_type == "custom_message":
            return _extract_pi_message_event(
                {"message": {"role": "custom", "content": event.get("content")}}
            )

        role = str(event.get("role", "")).strip().lower()
        if role in {"assistant", "user", "system"}:
            text = text_from_value(event.get("content"))
            if text:
                return NormalizedTranscriptEvent(
                    [TranscriptMessage(role=role, content=text)], is_boundary
                )

        return NormalizedTranscriptEvent([], is_boundary)


class JsonlTranscriptProvider(TranscriptProvider):
    """Generic JSONL provider for harness-native transcript files."""

    def supports(self, path: Path) -> bool:
        return path.name != HISTORY_FILENAME

    def iter_events(self, path: Path) -> Iterator[dict[str, object]]:
        yield from _iter_json_events(path)


def _iter_json_events(
    path: Path,
    *,
    current: Callable[[], bool] | None = None,
    validation: TranscriptValidation | None = None,
) -> Iterator[dict[str, object]]:
    with path.open("rb") as handle:
        while True:
            raw = read_jsonl_frame(handle, current=current)
            if not raw:
                return
            stripped = raw.strip()
            if stripped:
                reject_unframed_storage_frame(stripped, validation)
            if not raw.endswith(b"\n"):
                return
            if not stripped:
                continue
            try:
                payload_obj = json.loads(stripped.decode("utf-8", errors="ignore"))
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
        if event["event_type"] != "retained/native":
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


def _extract_opencode_db_system_prologue(event: dict[str, object]) -> str | None:
    return text_from_value(event.get("opencode_db_setup")) or None


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


@dataclass
class TranscriptNormalizer:
    """Canonical resumable setup/compaction interpretation, independent of accumulation."""

    setup: str | None = None
    pending_summary: str | None = None
    pi_session: bool = False
    pi_previous_entry_id: str | None = None
    rendering_reason: str | None = None
    opencode_user_seen: bool = False

    def _pi_journal(
        self, event: dict[str, object], messages: list[TranscriptMessage]
    ) -> NormalizedTranscriptEvent | None:
        event_type = event.get("type")
        if event_type == "session" and isinstance(event.get("id"), str) and "cwd" in event:
            self.pi_session = True
            self.pi_previous_entry_id = None
            version = event.get("version", 1)
            if type(version) is not int or version not in (1, 2, 3):
                self.rendering_reason = "Unsupported Pi session version; rendering is incomplete."
            return NormalizedTranscriptEvent([])
        entry_id = event.get("id")
        native_entry = isinstance(entry_id, str) and "parentId" in event
        if not self.pi_session and not (
            native_entry
            and event_type
            in (
                "message",
                "compaction",
                "branch_summary",
                "custom_message",
                "model_change",
                "thinking_level_change",
                "custom",
                "label",
                "session_info",
            )
            and (event_type != "message" or isinstance(event.get("message"), dict))
        ):
            return None
        if not native_entry and not isinstance(event_type, str):
            return None
        self.pi_session = True
        annotations: list[TranscriptMessage] = []
        if event_type == "message" and "message" not in event:
            self.rendering_reason = "Malformed Pi message; rendering is incomplete."
        if isinstance(entry_id, str) and len(entry_id) > 128:
            self.pi_previous_entry_id = None
            self.rendering_reason = "Unsupported Pi entry identity; rendering is incomplete."
            native_entry = False
        if native_entry:
            if (
                self.pi_previous_entry_id is not None
                and event.get("parentId") != self.pi_previous_entry_id
            ):
                annotations.append(
                    TranscriptMessage(
                        "annotation",
                        "Pi journal parent changed; continuing a different branch.",
                        kind="annotation",
                    )
                )
            self.pi_previous_entry_id = cast("str", entry_id)
        if event_type == "compaction":
            self.setup = text_from_value(event.get("summary")) or None
            self.pending_summary = None
            if not isinstance(event.get("summary"), str):
                self.rendering_reason = (
                    "Unsupported Pi compaction summary; rendering is incomplete."
                )
            return NormalizedTranscriptEvent(annotations, boundary=True)
        if event_type == "branch_summary":
            summary = text_from_value(event.get("summary"))
            annotations.append(
                TranscriptMessage(
                    "annotation",
                    f"Pi branch summary:\n{summary}",
                    kind="annotation",
                )
            )
            if not isinstance(event.get("summary"), str):
                self.rendering_reason = "Unsupported Pi branch summary; rendering is incomplete."
        elif event_type not in (
            "message",
            "custom_message",
            "model_change",
            "thinking_level_change",
            "custom",
            "label",
            "session_info",
        ):
            self.rendering_reason = "Unsupported Pi journal entry; rendering is incomplete."
        return NormalizedTranscriptEvent([*annotations, *messages])

    def feed(
        self, event: dict[str, object], parser: TranscriptEventParser
    ) -> NormalizedTranscriptEvent:
        normalized_event = _unwrap_seq_envelope(event)
        if normalized_event.get("record") == "opencode.transcript":
            events, is_user, reason = interpret_opencode_record(
                normalized_event, include_user_setup=not self.opencode_user_seen
            )
            self.opencode_user_seen |= is_user
            self.rendering_reason = reason or self.rendering_reason
            messages: list[TranscriptMessage] = []
            boundary = consumed_setup = False
            for projected in events:
                normalized = self.feed(projected, parser)
                messages.extend(normalized.messages)
                boundary |= normalized.boundary
                consumed_setup |= normalized.consumed_setup
            return NormalizedTranscriptEvent(messages, boundary, consumed_setup)
        extracted = parser.parse(event)
        messages, parser_boundary = extracted.messages, extracted.boundary
        self.rendering_reason = extracted.rendering_reason or self.rendering_reason
        pi_event = self._pi_journal(normalized_event, messages)
        if pi_event is not None:
            return pi_event
        opencode_boundary = _is_opencode_compaction_boundary(normalized_event)
        claude_boundary = _is_claude_compaction_boundary(normalized_event)
        if parser_boundary or opencode_boundary:
            self.setup = (
                _extract_claude_boundary_handoff(normalized_event) if claude_boundary else None
            )
            self.pending_summary = (
                ("claude" if claude_boundary else "opencode" if opencode_boundary else None)
                if self.setup is None
                else None
            )
            return NormalizedTranscriptEvent([], boundary=True)

        if self.pending_summary is not None:
            setup = (
                _extract_claude_follow_on_handoff(normalized_event, messages)
                if self.pending_summary == "claude"
                else _extract_opencode_follow_on_handoff(normalized_event, messages)
            )
            self.pending_summary = None
            if setup:
                self.setup = setup
                return NormalizedTranscriptEvent([], consumed_setup=True)

        if self.setup is None:
            self.setup = _extract_claude_system_prologue(
                normalized_event
            ) or _extract_opencode_db_system_prologue(normalized_event)
        return NormalizedTranscriptEvent(messages)


def _parse_events_with_prologues(
    events: Iterable[dict[str, object]],
    *,
    parser: TranscriptEventParser,
) -> TranscriptParseResult:
    segments: list[list[TranscriptMessage]] = [[]]
    segment_setups: list[str | None] = [None]
    consumed_setup_event_indexes: list[int] = []
    normalizer = TranscriptNormalizer()
    for event_index, event in enumerate(events):
        normalized = normalizer.feed(event, parser)
        if normalized.boundary:
            segments.append(list(normalized.messages))
            segment_setups.append(normalizer.setup)
        else:
            segment_setups[-1] = normalizer.setup
            segments[-1].extend(normalized.messages)
        if normalized.consumed_setup:
            consumed_setup_event_indexes.append(event_index)
    return TranscriptParseResult(
        segments=segments,
        total_compactions=len(segments) - 1,
        segment_setups=tuple(segment_setups),
        consumed_setup_event_indexes=tuple(consumed_setup_event_indexes),
        rendering_reason=normalizer.rendering_reason,
    )


def parse_transcript_events(
    events: Sequence[dict[str, object]],
    *,
    parser: TranscriptEventParser | None = None,
) -> tuple[list[list[TranscriptMessage]], int]:
    parsed = parse_transcript_events_with_prologues(events, parser=parser)
    return parsed.segments, parsed.total_compactions


def parse_transcript_events_with_prologues(
    events: Iterable[dict[str, object]],
    *,
    parser: TranscriptEventParser | None = None,
) -> TranscriptParseResult:
    resolved_parser = parser or DefaultTranscriptEventParser()
    return _parse_events_with_prologues(events, parser=resolved_parser)


def is_native_snapshot(path: Path) -> bool:
    """Bounded storage selection only; a true result is not a validated capture."""
    if path.name == NATIVE_SNAPSHOT_FILENAME:
        return True
    with path.open("rb") as handle:
        return is_snapshot_prefix(handle.readline(HEADER_LIMIT + 1))


def iter_transcript_events(
    path: Path,
    *,
    validation: TranscriptValidation | None = None,
    current: Callable[[], bool] | None = None,
    check_header: Callable[[SnapshotHeader], None] | None = None,
) -> Iterator[dict[str, object]]:
    # A copied/renamed snapshot keeps its storage identity. Sniff only a bounded
    # header; body validation remains incremental and subject to the caller budget.
    if path.is_file():
        with path.open("rb") as handle:
            if current is not None and not current():
                if validation is not None:
                    validation.state = "partial"
                    validation.reason = "Transcript read paused before header selection"
                return
            first = handle.readline(HEADER_LIMIT + 1)
            if path.name == NATIVE_SNAPSHOT_FILENAME or is_snapshot_prefix(first):
                handle.seek(0)
                yield from read_snapshot(
                    handle,
                    validation=validation or TranscriptValidation(),
                    current=current,
                    check_header=check_header,
                )
                return
    try:
        provider = _provider_for_path(path)
        if isinstance(provider, HistoryJsonlTranscriptProvider):
            stream: Iterator[dict[str, object]] = iter_history_events(
                path,
                current=current,
                frame_guard=lambda raw: reject_unframed_storage_frame(raw, validation),
            )
        elif isinstance(provider, JsonlTranscriptProvider):
            stream = _iter_json_events(path, current=current, validation=validation)
        else:
            stream = provider.iter_events(path)
        for event in stream:
            reject_unframed_storage_record(event, validation)
            yield event
        if validation is not None:
            validation.state = "complete"
            validation.reason = None
    except TranscriptReadPaused:
        if validation is not None:
            validation.state = "partial"
            validation.reason = "Transcript read paused before complete EOF"
    except ValueError as exc:
        if validation is not None and validation.state != "corrupt":
            validation.state = "corrupt"
            validation.reason = str(exc)[:1024]
        raise


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


def parse_opencode_db_transcript_with_prologues(
    session_id: str,
    *,
    parser: TranscriptEventParser | None = None,
) -> TranscriptParseResult:
    resolved_parser = parser or DefaultTranscriptEventParser()
    return _parse_events_with_prologues(
        iter_opencode_db_events(session_id=session_id),
        parser=resolved_parser,
    )


__all__ = [
    "DefaultTranscriptEventParser",
    "HistoryJsonlTranscriptProvider",
    "JsonlTranscriptProvider",
    "OpenCodeStorageTranscriptProvider",
    "ToolCall",
    "TranscriptEventParser",
    "TranscriptMessage",
    "TranscriptParseResult",
    "TranscriptProvider",
    "_text_from_value",
    "iter_transcript_events",
    "parse_opencode_db_transcript_with_prologues",
    "parse_transcript_events",
    "parse_transcript_events_with_prologues",
    "parse_transcript_file",
    "parse_transcript_file_with_prologues",
    "text_from_value",
]


def transcript_revision(path: Path | None) -> tuple[tuple[int, ...] | None, ...]:
    """Cheap provider freshness witness; OpenCode storage may be backed by a mutable DB."""
    from meridian.lib.harness.opencode_transcript import (
        opencode_db_for_session_file,
        resolve_opencode_db_path,
    )

    paths = [] if path is None else [path]
    if path is None or isinstance(_provider_for_path(path), OpenCodeStorageTranscriptProvider):
        database = opencode_db_for_session_file(path) if path else resolve_opencode_db_path()
        assert database is not None
        paths.extend((database, Path(str(database) + "-wal")))
    revisions: list[tuple[int, ...] | None] = []
    for source in paths:
        try:
            info = source.stat()
        except FileNotFoundError:
            revisions.append(None)
        else:
            revisions.append(
                (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            )
    return tuple(revisions)
