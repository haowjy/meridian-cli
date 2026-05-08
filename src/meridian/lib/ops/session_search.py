"""Session search operation across all compaction segments."""

from __future__ import annotations

import shlex
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.config.project_root import resolve_project_root
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.runtime import (
    async_from_sync,
    resolve_runtime_root_for_read,
)
from meridian.lib.ops.session_log import parse_session_file
from meridian.lib.ops.session_target import resolve_session_log_target

_PREVIEW_LIMIT = 200
_NAV_WINDOW_SIZE = 10
_NAV_CENTER_OFFSET = 5


class SessionSearchInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str = ""
    ref: str = ""
    file_path: str | None = None
    project_root: str | None = None


class SessionSearchMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    segment: int
    message_index: int
    role: str
    content_preview: str
    nav_command: str


class SessionSearchOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    matches: tuple[SessionSearchMatch, ...]

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        if not self.matches:
            return f"Session {self.session_id} — no matches"

        match_label = "match" if len(self.matches) == 1 else "matches"
        lines = [f"Session {self.session_id} — {len(self.matches)} {match_label}"]
        for match in self.matches:
            lines.append("")
            lines.append(
                f"--- segment {match.segment}, message {match.message_index} [{match.role}] ---"
            )
            lines.append(match.content_preview)
            lines.append(f"Navigate: {match.nav_command}")
        return "\n".join(lines)


def _normalize_content(value: str) -> str:
    return " ".join(value.split())


def _build_preview(content: str, *, query: str, limit: int = _PREVIEW_LIMIT) -> str:
    if not content:
        return ""

    normalized_query = query.lower()
    lowered = content.lower()
    match_start = lowered.find(normalized_query)
    if match_start < 0:
        return content if len(content) <= limit else f"{content[: limit - 3].rstrip()}..."

    if len(content) <= limit:
        window_start = 0
        window_end = len(content)
    else:
        half_context = max((limit - len(query)) // 2, 0)
        window_start = max(match_start - half_context, 0)
        window_end = min(window_start + limit, len(content))
        window_start = max(window_end - limit, 0)

    snippet = content[window_start:window_end]
    local_start = match_start - window_start
    local_end = local_start + len(query)
    highlighted = (
        f"{snippet[:local_start]}[[{snippet[local_start:local_end]}]]"
        f"{snippet[local_end:]}"
    )

    prefix = "..." if window_start > 0 else ""
    suffix = "..." if window_end < len(content) else ""
    return f"{prefix}{highlighted}{suffix}"


def _build_nav_command(
    *,
    payload: SessionSearchInput,
    resolved_session_id: str,
    segment: int,
    message_index: int,
    segment_message_count: int,
) -> str:
    offset = max(segment_message_count - message_index - _NAV_CENTER_OFFSET, 0)
    if payload.file_path is not None and payload.file_path.strip():
        return (
            f"meridian session log --file {shlex.quote(payload.file_path.strip())} "
            f"-c {segment} --offset {offset} --last {_NAV_WINDOW_SIZE}"
        )

    ref = payload.ref.strip() or resolved_session_id
    return (
        f"meridian session log {shlex.quote(ref)} "
        f"-c {segment} --offset {offset} --last {_NAV_WINDOW_SIZE}"
    )


def session_search_sync(
    payload: SessionSearchInput,
    ctx: RuntimeContext | None = None,
) -> SessionSearchOutput:
    _ = ctx
    query = payload.query.strip()
    if not query:
        raise ValueError("query must not be empty")

    explicit_project_root = (
        Path(payload.project_root).expanduser().resolve() if payload.project_root else None
    )
    project_root = resolve_project_root(explicit_project_root)
    runtime_root = resolve_runtime_root_for_read(project_root)

    target = resolve_session_log_target(
        ref=payload.ref,
        file_path=payload.file_path,
        project_root=project_root,
        runtime_root=runtime_root,
    )
    segments, total_compactions = parse_session_file(target.file_path)

    query_lower = query.lower()
    matches: list[SessionSearchMatch] = []
    for segment_index, messages in enumerate(segments):
        segment = total_compactions - segment_index
        segment_message_count = len(messages)
        for message_index, message in enumerate(messages, start=1):
            normalized_content = _normalize_content(message.content)
            if not normalized_content:
                continue
            if query_lower not in normalized_content.lower():
                continue
            matches.append(
                SessionSearchMatch(
                    segment=segment,
                    message_index=message_index,
                    role=message.role,
                    content_preview=_build_preview(normalized_content, query=query),
                    nav_command=_build_nav_command(
                        payload=payload,
                        resolved_session_id=target.session_id,
                        segment=segment,
                        message_index=message_index,
                        segment_message_count=segment_message_count,
                    ),
                )
            )

    return SessionSearchOutput(
        session_id=target.session_id,
        matches=tuple(matches),
    )


session_search = async_from_sync(session_search_sync)


__all__ = [
    "SessionSearchInput",
    "SessionSearchMatch",
    "SessionSearchOutput",
    "session_search",
    "session_search_sync",
]
