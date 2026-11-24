"""Reading ``agy``'s native transcript for ``session log``, search and ``--from``.

Google documents none of this format; it is what ``agy`` 1.5.13 (the only version seen)
writes to
``<store>/<conversation>/.system_generated/logs/transcript_full.jsonl`` while a turn runs and
keep after exit. ``transcript.jsonl`` beside it has the same steps with long fields cut
(``truncated_fields``), so only ``transcript_full.jsonl`` is read. One JSON object per step,
``step_index`` rising, ``type`` one of:

- ``USER_INPUT``: ``<USER_REQUEST>text</USER_REQUEST>`` plus metadata tags. ``source`` is
  ``USER_EXPLICIT`` for what the person typed, ``SYSTEM_SDK`` for a hook's injected step.
- ``PLANNER_RESPONSE``: the model's ``content`` (may be null) and ``tool_calls``
  (``{name, args}``); its ``thinking`` is not rendered.
- ``GENERIC``: the result of a tool call, as ``content`` after ``Created At:`` lines.
- ``SYSTEM_MESSAGE``: text a hook or the harness injected, inside ``<SYSTEM_MESSAGE>``.
- ``CHECKPOINT``: the summary after a compaction. ``ERROR_MESSAGE``: a failed model call.

``context.py`` and the ``meridian-session`` hook's ``guard.sh`` match ``CHECKPOINT`` and
``SYSTEM_MESSAGE`` lines by their raw bytes; a format change for a newer ``agy`` updates all three.

An unknown ``type`` is skipped and flagged through ``rendering_reason``, so a newer ``agy``
never fails a read. The generic JSONL reader already drops malformed lines and an unfinished
last line.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

from meridian.lib.harness.adapter import BaseNativeStoreReader
from meridian.lib.harness.agy.state import is_safe_id
from meridian.lib.harness.transcript import (
    INJECTED_ROLE,
    NormalizedTranscriptEvent,
    ToolCall,
    TranscriptEventParser,
    TranscriptMessage,
    normalize_tool,
    preview,
)

# Tags on a line of their own: the system preamble mentions <SYSTEM_MESSAGE> inline.
_USER_REQUEST = re.compile(r"^<USER_REQUEST>\s*(.*)\s*^</USER_REQUEST>", re.DOTALL | re.MULTILINE)
_SYSTEM_MESSAGE = re.compile(
    r"^<SYSTEM_MESSAGE>\s*(.*)\s*^</SYSTEM_MESSAGE>", re.DOTALL | re.MULTILINE
)
_TOOL_RESULT_HEADER = re.compile(r"\A(?:(?:Created|Completed) At: [^\n]*\n)+")
# Argument names that carry what a tool call is about, in the order they are tried.
_BODY_KEYS = ("CommandLine", "Path", "query", "Prompt", "Message", "toolSummary")
_TOOL_KINDS = {
    "run_command": "bash",
    "read_file": "read",
    "write_file": "write",
    "edit_file": "edit",
}
_UNRECOGNISED = "Unrecognised Antigravity transcript entry; rendering is incomplete."


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _tagged(pattern: re.Pattern[str], text: str) -> str:
    found = pattern.search(text)
    return (found.group(1) if found else text).strip()


def _tool_call(raw: object) -> TranscriptMessage | None:
    if not isinstance(raw, dict):
        return None
    call = cast("dict[str, object]", raw)
    name = _text(call.get("name"))
    args = call.get("args")
    if not name:
        return None
    arguments = cast("dict[str, object]", args) if isinstance(args, dict) else {}
    if name == "call_mcp_tool":
        server, tool = _text(arguments.get("ServerName")), _text(arguments.get("ToolName"))
        body = json.dumps(arguments.get("Arguments"), sort_keys=True, separators=(",", ":"))
        tool_call = ToolCall("mcp", f"{server}.{tool} {body}")
    else:
        body = next((_text(arguments[key]) for key in _BODY_KEYS if _text(arguments.get(key))), "")
        kind = _TOOL_KINDS.get(name)
        tool_call = ToolCall(kind, body) if kind is not None else normalize_tool(name, body)
    marker = f"[tool: {name} {preview(tool_call.body)}]" if tool_call.body else f"[tool: {name}]"
    return TranscriptMessage("assistant", marker, tool_call)


class AgyTranscriptParser(TranscriptEventParser):
    def parse(self, event: dict[str, object]) -> NormalizedTranscriptEvent:
        kind = event.get("type")
        content = _text(event.get("content"))
        if kind == "USER_INPUT":
            role = "user" if event.get("source") == "USER_EXPLICIT" else INJECTED_ROLE
            text = _tagged(_USER_REQUEST, content)
            return NormalizedTranscriptEvent([TranscriptMessage(role, text)] if text else [])
        if kind == "SYSTEM_MESSAGE":
            text = _tagged(_SYSTEM_MESSAGE, content)
            return NormalizedTranscriptEvent(
                [TranscriptMessage(INJECTED_ROLE, text)] if text else []
            )
        if kind == "CHECKPOINT":
            return NormalizedTranscriptEvent(
                [TranscriptMessage(INJECTED_ROLE, content)] if content else [], boundary=True
            )
        if kind == "ERROR_MESSAGE":
            text = content or _text(event.get("error"))
            return NormalizedTranscriptEvent(
                [TranscriptMessage("annotation", text, kind="annotation")] if text else []
            )
        if kind == "GENERIC":
            text = _TOOL_RESULT_HEADER.sub("", content, count=1).strip()
            result = TranscriptMessage("user", f"[tool_result] {text}".strip(), is_tool_result=True)
            return NormalizedTranscriptEvent([result])
        if kind == "PLANNER_RESPONSE":
            messages = [TranscriptMessage("assistant", content)] if content else []
            calls = event.get("tool_calls")
            for raw in cast("list[object]", calls) if isinstance(calls, list) else []:
                call = _tool_call(raw)
                if call is not None:
                    messages.append(call)
            return NormalizedTranscriptEvent(messages)
        return NormalizedTranscriptEvent([], rendering_reason=_UNRECOGNISED)


def transcript_path(native_store: Path, session_id: str) -> Path:
    return native_store / session_id / ".system_generated" / "logs" / "transcript_full.jsonl"


class AgyReader(BaseNativeStoreReader):
    def transcript_parser(self) -> TranscriptEventParser:
        return AgyTranscriptParser()

    def resolve_native_session_file(self, *, session_id: str, native_store: Path) -> Path | None:
        if not is_safe_id(session_id):
            return None
        path = transcript_path(native_store, session_id)
        return path if path.is_file() else None
