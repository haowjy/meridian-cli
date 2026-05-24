"""Session log operation with deterministic transcript navigation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.runtime import async_from_sync
from meridian.lib.ops.session_render import (
    showing_window,
    window_from_around_context,
    window_from_before_limit,
    window_from_from_limit,
    window_from_tail,
)
from meridian.lib.ops.session_transcript import (
    AbsoluteTranscriptMessage,
    build_session_log_command,
    read_session_transcript,
)


class SessionLogInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str = ""
    segment: str | None = None
    tail: int | None = None
    from_ordinal: int | None = None
    before_ordinal: int | None = None
    around_ordinal: int | None = None
    limit: int | None = None
    context: int | None = None
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
    requested_ref: str | None = None
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
        source = self.source or ""
        normalized_requested_ref = (self.requested_ref or "").strip()
        if normalized_requested_ref and normalized_requested_ref != self.session_id:
            session_label = (
                f"{normalized_requested_ref} ({source}: {self.session_id})"
                if source
                else f"{normalized_requested_ref} ({self.session_id})"
            )
        else:
            session_label = f"{self.session_id} ({source})" if source else self.session_id
        message_label = "message" if self.total_messages == 1 else "messages"
        lines = [
            (
                f"Session {session_label} — showing "
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


def _window_hints(payload: SessionLogInput, *, uses_absolute_window: bool) -> tuple[str, ...]:
    if uses_absolute_window or payload.tail is not None:
        return ()
    return ("Use --tail [N] for recent messages.",)


def _resolve_segment_index(*, total_segments: int, segment: str | None) -> int:
    if total_segments <= 0:
        return 0

    normalized = (segment or "current").strip().lower()
    if normalized == "current":
        return total_segments - 1
    if normalized == "previous":
        if total_segments < 2:
            raise ValueError("No previous segment is available.")
        return total_segments - 2

    if not normalized.isdigit():
        raise ValueError("segment must be 'current', 'previous', or a non-negative integer.")

    segment_index = int(normalized)
    if segment_index >= total_segments:
        raise ValueError(
            f"Segment {segment_index} out of range (available: 0-{total_segments - 1})"
        )
    return segment_index


def _segment_label(*, selected_index: int, total_segments: int) -> str:
    if total_segments <= 0:
        return "segment 0"
    if selected_index == total_segments - 1:
        return f"segment {selected_index} (current)"
    if total_segments >= 2 and selected_index == total_segments - 2:
        return f"segment {selected_index} (previous)"
    return f"segment {selected_index}"


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
        if payload.segment is not None:
            raise ValueError("--segment cannot be combined with absolute windows.")

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

    parsed = read_session_transcript(
        ref=payload.ref,
        file_path=payload.file_path,
        project_root=payload.project_root,
    )

    selected_segment_index: int | None = None
    selected_segment_label: str | None = None
    selected_segment_messages: int | None = None

    if not uses_absolute_window:
        selected_segment_index = _resolve_segment_index(
            total_segments=len(parsed.segments),
            segment=payload.segment,
        )
        selected_segment_label = _segment_label(
            selected_index=selected_segment_index,
            total_segments=len(parsed.segments),
        )

        segment_messages = [
            message
            for message in parsed.messages
            if message.segment_index == selected_segment_index
        ]
        selected_segment_messages = len(segment_messages)
        page = window_from_tail(segment_messages, tail=payload.tail)
    elif payload.from_ordinal is not None:
        limit = payload.limit if payload.limit is not None else 0
        page = window_from_from_limit(
            parsed.messages,
            start_ordinal=payload.from_ordinal,
            limit=limit,
        )
    elif payload.before_ordinal is not None:
        limit = payload.limit if payload.limit is not None else 0
        page = window_from_before_limit(
            parsed.messages,
            before_ordinal=payload.before_ordinal,
            limit=limit,
        )
    else:
        around_ordinal = payload.around_ordinal if payload.around_ordinal is not None else 1
        context = payload.context if payload.context is not None else 0
        page = window_from_around_context(
            parsed.messages,
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
            previous_command = build_session_log_command(
                parsed.route,
                from_ordinal=page.previous_from,
                limit=nav_limit,
            )
        if page.next_from is not None:
            next_command = build_session_log_command(
                parsed.route,
                from_ordinal=page.next_from,
                limit=nav_limit,
            )

    output_messages = tuple(_message_row(item) for item in page.messages)

    return SessionLogOutput(
        session_id=parsed.target.session_id,
        requested_ref=payload.ref.strip() or None,
        source=parsed.target.source,
        total_messages=len(parsed.messages),
        total_segments=len(parsed.segments),
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
