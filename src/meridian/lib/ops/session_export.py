"""Export session transcripts as clean markdown."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.spawn_start import resolve_spawn_display_label
from meridian.lib.core.util import FormatContext
from meridian.lib.harness.transcript import TranscriptMessage
from meridian.lib.ops.runtime import async_from_sync
from meridian.lib.ops.session_transcript import read_session_transcript
from meridian.lib.state import session_identity, session_store, spawn_store

_TOOL_CALL_RE = re.compile(r"^\[tool:\s*(?P<name>[^\]\s]+)(?:\s+(?P<body>.*))?\]$", re.DOTALL)
_TOOL_RESULT_PREFIX = "[tool_result]"
_SHORT_TOOL_RESULT_MAX_LINES = 7
_LONG_TOOL_RESULT_PREVIEW_LINES = 5


class SessionExportInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str = ""
    file_path: str | None = None
    include_spawns: bool = False
    project_root: str | None = None


class SessionExportOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    markdown: str

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        return self.markdown


def _role_label(role: str) -> str:
    normalized = role.strip().lower()
    if normalized == "user":
        return "User"
    if normalized == "assistant":
        return "Assistant"
    return normalized.title() or "Message"


def _tool_summary(name: str, body: str) -> str:
    normalized = body.strip() or name
    first_line = normalized.splitlines()[0].strip()
    return first_line or name


def _blockquote(lines: list[str]) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def _render_tool_result(command: str | None, body: str) -> str:
    output = body.strip() or "(no output)"
    lines = output.splitlines()
    command_label = command or "tool result"
    if len(lines) <= _SHORT_TOOL_RESULT_MAX_LINES:
        return _blockquote(
            [
                f"`{command_label}`",
                "",
                "```text",
                *lines,
                "```",
            ]
        )

    preview = lines[:_LONG_TOOL_RESULT_PREVIEW_LINES]
    remaining = len(lines) - len(preview)
    return _blockquote(
        [
            f"`{command_label}` —",
            *preview,
            f"... {remaining} more lines",
        ]
    )


def _render_message(message: TranscriptMessage) -> str | None:
    role = message.role.strip().lower()
    content = message.content.strip()
    if not content:
        return None
    if role == "system":
        return None

    tool_call = _TOOL_CALL_RE.match(content)
    if tool_call is not None:
        name = tool_call.group("name").strip()
        body = (tool_call.group("body") or "").strip() or name
        return _blockquote([f"`{_tool_summary(name, body)}`"])

    if content.startswith(_TOOL_RESULT_PREFIX):
        body = content[len(_TOOL_RESULT_PREFIX) :].strip() or "(no output)"
        return _render_tool_result(None, body)

    return "\n".join([f"**{_role_label(role)}:**", "", content])


def _render_messages(messages: list[TranscriptMessage]) -> list[str]:
    rendered: list[str] = []
    pending_tool: str | None = None
    first_user_message = True
    for message in messages:
        role = message.role.strip().lower()
        content = message.content.strip()
        if not content or role == "system":
            continue

        tool_call = _TOOL_CALL_RE.match(content)
        if tool_call is not None:
            name = tool_call.group("name").strip()
            body = (tool_call.group("body") or "").strip() or name
            pending_tool = _tool_summary(name, body)
            continue

        if content.startswith(_TOOL_RESULT_PREFIX):
            body = content[len(_TOOL_RESULT_PREFIX) :].strip() or "(no output)"
            rendered.append(_render_tool_result(pending_tool, body))
            pending_tool = None
            continue

        if pending_tool is not None:
            rendered.append(_blockquote([f"`{pending_tool}`"]))
            pending_tool = None

        if role == "user":
            if not first_user_message:
                rendered.append("---")
            first_user_message = False
        rendered_message = _render_message(message)
        if rendered_message:
            rendered.append(rendered_message)

    if pending_tool is not None:
        rendered.append(_blockquote([f"`{pending_tool}`"]))
    return rendered


def _flatten_segments(segments: list[list[TranscriptMessage]]) -> list[TranscriptMessage]:
    return [message for segment in segments for message in segment]


def _parse_iso(value: str | None) -> datetime | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _format_duration(started_at: str | None, stopped_at: str | None) -> str | None:
    start = _parse_iso(started_at)
    stop = _parse_iso(stopped_at)
    if start is None or stop is None:
        return None
    seconds = max(int((stop - start).total_seconds()), 0)
    minutes, remainder = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {remainder}s"
    if minutes:
        return f"{minutes}m {remainder}s"
    return f"{remainder}s"


def _session_metadata(runtime_root: Path, ref: str) -> list[str]:
    record = session_store.resolve_session_ref(runtime_root, ref)
    spawn = spawn_store.get_spawn(runtime_root, ref) if ref.startswith("p") else None
    if record is None and spawn is not None and spawn.chat_id is not None:
        record = session_store.resolve_session_ref(runtime_root, spawn.chat_id)

    lines: list[str] = []
    started_at = record.started_at if record is not None else (spawn.started_at if spawn else None)
    stopped_at = (
        record.stopped_at
        if record is not None
        else (spawn.last_attempt_exited_at if spawn else None)
    )
    model = record.model if record is not None else (spawn.model if spawn else None)
    harness = record.harness if record is not None else (spawn.harness if spawn else None)
    if started_at:
        lines.append(f"- Date: {started_at}")
    if model:
        lines.append(f"- Model: {model}")
    if harness:
        lines.append(f"- Harness: {harness}")
    duration = _format_duration(started_at, stopped_at)
    if duration:
        lines.append(f"- Duration: {duration}")
    return lines


def _session_scope_for_ref(
    runtime_root: Path,
    ref: str,
) -> tuple[str | None, str | None, str | None]:
    """Return (exact_chat_id, owner_chat_id, parent_spawn_id) for export grouping."""

    normalized = ref.strip()
    if session_identity.is_tracked_chat_ref(runtime_root, normalized):
        exact_chat_id = normalized
        owner_chat_id = session_identity.get_owner_chat_for_session(runtime_root, exact_chat_id)
        return exact_chat_id, owner_chat_id, None
    if normalized.startswith("p") and normalized[1:].isdigit():
        spawn = spawn_store.get_spawn(runtime_root, normalized)
        if spawn is None:
            return None, None, normalized
        return (
            session_identity.spawn_exact_chat_id(spawn),
            session_identity.spawn_owner_chat_id(spawn),
            normalized,
        )
    record = session_store.resolve_session_ref(runtime_root, normalized)
    if record is None:
        return None, None, None
    return (
        session_identity.session_exact_chat_id(record),
        session_identity.session_owner_chat_id(runtime_root, record),
        None,
    )


def _spawn_appendices(
    runtime_root: Path,
    *,
    exact_chat_id: str | None,
    owner_chat_id: str | None,
    parent_id: str | None,
) -> list[str]:
    if exact_chat_id is None and owner_chat_id is None and parent_id is None:
        return []
    sections: list[str] = []
    seen: set[str] = set()
    for spawn in spawn_store.list_spawns(runtime_root):
        if spawn.id in seen:
            continue
        if spawn.kind == "primary":
            continue
        included = parent_id is not None and spawn.parent_id == parent_id
        if not included and exact_chat_id is not None:
            included = session_identity.spawn_exact_chat_id(spawn) == exact_chat_id
        if not included and owner_chat_id is not None:
            included = session_identity.spawn_matches_owner_chat(spawn, owner_chat_id)
        if not included:
            continue
        report_path = runtime_root / "spawns" / spawn.id / "report.md"
        if not report_path.is_file():
            continue
        report = report_path.read_text(encoding="utf-8", errors="ignore").strip()
        if not report:
            continue
        seen.add(spawn.id)
        desc_source = resolve_spawn_display_label(spawn.goal, spawn.desc, spawn.display_label) or ""
        desc = desc_source.splitlines()[0] if desc_source else ""
        suffix = f" — {desc}" if desc else ""
        sections.append("\n".join([f"## Spawn: {spawn.id}{suffix}", "", report]))
    return sections


def _render_markdown(
    *,
    session_id: str,
    source: str,
    metadata: list[str],
    messages: list[TranscriptMessage],
    appendices: list[str],
) -> str:
    parts = [f"# Session {session_id}", ""]
    meta = [f"- Source: {source}", *metadata]
    if meta:
        parts.extend(meta)
        parts.append("")
    parts.extend(_render_messages(messages))
    if appendices:
        parts.extend(["", "## Spawn Reports", "", *appendices])
    return "\n\n".join(part for part in parts if part != "").rstrip() + "\n"


def session_export_sync(
    payload: SessionExportInput,
    ctx: RuntimeContext | None = None,
) -> SessionExportOutput:
    _ = ctx
    transcript = read_session_transcript(
        ref=payload.ref,
        file_path=payload.file_path,
        project_root=payload.project_root,
    )
    ref = payload.ref.strip() or transcript.target.session_id
    runtime_root = transcript.runtime_root
    exact_chat_id, owner_chat_id, parent_id = (
        _session_scope_for_ref(runtime_root, ref)
        if runtime_root is not None
        else (None, None, None)
    )
    appendices = (
        _spawn_appendices(
            runtime_root,
            exact_chat_id=exact_chat_id,
            owner_chat_id=owner_chat_id,
            parent_id=parent_id,
        )
        if payload.include_spawns and runtime_root is not None
        else []
    )
    markdown = _render_markdown(
        session_id=transcript.target.session_id,
        source=transcript.target.source,
        metadata=_session_metadata(runtime_root, ref) if runtime_root is not None else [],
        messages=_flatten_segments(transcript.segments),
        appendices=appendices,
    )
    return SessionExportOutput(session_id=transcript.target.session_id, markdown=markdown)


session_export = async_from_sync(session_export_sync)


__all__ = [
    "SessionExportInput",
    "SessionExportOutput",
    "session_export",
    "session_export_sync",
]
