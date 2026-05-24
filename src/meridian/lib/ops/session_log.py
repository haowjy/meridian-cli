"""Session log operation with deterministic transcript navigation."""

from __future__ import annotations

import shlex

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.runtime import async_from_sync
from meridian.lib.ops.session_read import read_session_transcript
from meridian.lib.ops.session_render import (
    AbsoluteTranscriptMessage,
    flatten_transcript_segments,
    paginate_recent_messages,
    resolve_segment_index,
    segment_label,
    showing_window,
    window_from_around_context,
    window_from_before_limit,
    window_from_from_limit,
)


class SessionLogInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str = ""
    segment: str | None = None
    compaction: int | None = None
    tail: int | None = None
    from_ordinal: int | None = None
    before_ordinal: int | None = None
    around_ordinal: int | None = None
    limit: int | None = None
    context: int | None = None
    last_n: int | None = None
    offset: int = 0
    file_path: str | None = None
    project_root: str | None = None


class SessionLogMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    segment: int
    segment_message: int
    role: str
    content: str


class SessionLogOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    source: str | None = None
    total_messages: int
    total_segments: int
    segment_index: int | None = None
    segment_messages: int | None = None
    segment_label: str | None = None
    showing: str
    messages: tuple[SessionLogMessage, ...]
    previous_command: str | None = None
    next_command: str | None = None
    hints: tuple[str, ...] = ()

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        source = f" ({self.source})" if self.source else ""
        message_label = "message" if self.total_messages == 1 else "messages"
        lines = [
            (
                f"Session {self.session_id}{source} — showing "
                f"{self.showing} of {self.total_messages} {message_label}"
            )
        ]
        if self.segment_label is not None and self.segment_messages is not None:
            lines.append(f"{self.segment_label}; {self.segment_messages} messages in segment")
        for message in self.messages:
            lines.append("")
            lines.append(
                f"--- {message.index} [segment {message.segment} · "
                f"message {message.segment_message}] [{message.role}] ---"
            )
            lines.append(message.content)
        nav_lines: list[str] = []
        if self.previous_command is not None:
            nav_lines.append(f"Previous: {self.previous_command}")
        if self.next_command is not None:
            nav_lines.append(f"Next: {self.next_command}")
        if nav_lines:
            lines.append("")
            lines.extend(nav_lines)
        if self.hints:
            lines.append("")
            lines.extend(self.hints)
        return "\n".join(lines)


def _nav_ref(payload: SessionLogInput, *, resolved_session_id: str) -> str:
    normalized_ref = payload.ref.strip()
    return normalized_ref or resolved_session_id


def _build_log_command(
    payload: SessionLogInput,
    *,
    resolved_session_id: str,
    from_ordinal: int,
    limit: int,
) -> str:
    if payload.file_path is not None and payload.file_path.strip():
        return (
            f"meridian session log --file {shlex.quote(payload.file_path.strip())} "
            f"--from {from_ordinal} --limit {limit}"
        )
    ref = _nav_ref(payload, resolved_session_id=resolved_session_id)
    return f"meridian session log {shlex.quote(ref)} --from {from_ordinal} --limit {limit}"


def _window_hints(payload: SessionLogInput, *, uses_absolute_window: bool) -> tuple[str, ...]:
    if uses_absolute_window:
        return ()
    hints: list[str] = []
    if payload.tail is not None:
        hints.append("Use --tail N to adjust recent-message view size.")
    elif payload.last_n is not None or payload.offset > 0:
        hints.append("Legacy window mode: --last/--offset.")
        hints.append("Prefer --tail, --from/--limit, or --around/--context.")
    else:
        hints.append("Use --tail for recent messages.")
    return tuple(hints)


def _message_row(message: AbsoluteTranscriptMessage) -> SessionLogMessage:
    return SessionLogMessage(
        index=message.ordinal,
        segment=message.segment_index,
        segment_message=message.segment_message_index,
        role=message.role,
        content=message.content,
    )


def session_log_sync(
    payload: SessionLogInput,
    ctx: RuntimeContext | None = None,
) -> SessionLogOutput:
    _ = ctx
    if payload.tail is not None and payload.tail < 0:
        raise ValueError("--tail must be >= 0.")

    absolute_selectors = [
        payload.from_ordinal is not None,
        payload.before_ordinal is not None,
        payload.around_ordinal is not None,
    ]
    if sum(absolute_selectors) > 1:
        raise ValueError("Use only one of --from, --before, or --around.")

    uses_absolute_window = any(absolute_selectors)
    if uses_absolute_window:
        if payload.tail is not None:
            raise ValueError("--tail cannot be combined with --from/--before/--around.")
        if payload.last_n is not None or payload.offset > 0:
            raise ValueError(
                "--last/--offset cannot be combined with --from/--before/--around."
            )
        if payload.segment is not None or payload.compaction is not None:
            raise ValueError("--segment/--compaction cannot be combined with absolute windows.")
    if payload.context is not None and payload.around_ordinal is None:
        raise ValueError("--context requires --around.")
    if payload.limit is not None and not (payload.from_ordinal or payload.before_ordinal):
        raise ValueError("--limit requires --from or --before.")
    if payload.from_ordinal is not None and payload.limit is None:
        raise ValueError("--from requires --limit.")
    if payload.before_ordinal is not None and payload.limit is None:
        raise ValueError("--before requires --limit.")
    if payload.around_ordinal is not None and payload.context is None:
        raise ValueError("--around requires --context.")

    read = read_session_transcript(
        ref=payload.ref,
        file_path=payload.file_path,
        project_root=payload.project_root,
    )
    flattened = flatten_transcript_segments(read.segments)

    selected_segment_index: int | None = None
    selected_segment_label: str | None = None
    selected_segment_messages: int | None = None
    if not uses_absolute_window:
        selected_segment_index = resolve_segment_index(
            segments=read.segments,
            segment=payload.segment,
            compaction=payload.compaction,
        )
        selected_segment_label = segment_label(
            selected_index=selected_segment_index,
            total_segments=len(read.segments),
        )

        segment_filtered = [
            message for message in flattened if message.segment_index == selected_segment_index
        ]
        selected_segment_messages = len(segment_filtered)
        if payload.tail is not None:
            page = paginate_recent_messages(segment_filtered, last_n=payload.tail, offset=0)
        else:
            page = paginate_recent_messages(
                segment_filtered,
                last_n=payload.last_n,
                offset=payload.offset,
            )
    elif payload.from_ordinal is not None:
        limit = payload.limit if payload.limit is not None else 0
        page = window_from_from_limit(flattened, start_ordinal=payload.from_ordinal, limit=limit)
    elif payload.before_ordinal is not None:
        limit = payload.limit if payload.limit is not None else 0
        page = window_from_before_limit(
            flattened,
            before_ordinal=payload.before_ordinal,
            limit=limit,
        )
    else:
        around_ordinal = payload.around_ordinal if payload.around_ordinal is not None else 1
        context = payload.context if payload.context is not None else 0
        page = window_from_around_context(
            flattened,
            around_ordinal=around_ordinal,
            context=context,
        )

    previous_command: str | None = None
    next_command: str | None = None
    if uses_absolute_window:
        nav_limit = (
            payload.limit
            if payload.limit is not None
            else (payload.context if payload.context is not None else 5) * 2 + 1
        )
        if page.previous_from is not None:
            previous_command = _build_log_command(
                payload,
                resolved_session_id=read.target.session_id,
                from_ordinal=page.previous_from,
                limit=nav_limit,
            )
        if page.next_from is not None:
            next_command = _build_log_command(
                payload,
                resolved_session_id=read.target.session_id,
                from_ordinal=page.next_from,
                limit=nav_limit,
            )

    output_messages = tuple(_message_row(item) for item in page.messages)

    return SessionLogOutput(
        session_id=read.target.session_id,
        source=read.target.source,
        total_messages=len(flattened),
        total_segments=len(read.segments),
        segment_index=selected_segment_index,
        segment_messages=selected_segment_messages,
        segment_label=selected_segment_label,
        showing=showing_window(page.start_ordinal, page.end_ordinal),
        messages=output_messages,
        previous_command=previous_command,
        next_command=next_command,
        hints=_window_hints(payload, uses_absolute_window=uses_absolute_window),
    )


session_log = async_from_sync(session_log_sync)


__all__ = [
    "SessionLogInput",
    "SessionLogMessage",
    "SessionLogOutput",
    "session_log",
    "session_log_sync",
]
