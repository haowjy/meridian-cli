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
    AbsoluteTranscriptEntry,
    AbsoluteTranscriptMessage,
    build_session_log_command,
    read_session_transcript,
)

_CONTENT_PREVIEW_MAX_LINES = 80
_CONTENT_PREVIEW_MAX_CHARS = 8000


class SessionLogInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str = ""
    segment: str | None = None
    full: bool = False
    truncate: bool = True
    tail: int | None = None
    from_ordinal: int | None = None
    before_ordinal: int | None = None
    around_ordinal: int | None = None
    limit: int | None = None
    context: int | None = None
    file_path: str | None = None
    project_root: str | None = None


class SessionLogEntryMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    segment_message: int
    role: str
    content: str


class SessionLogEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    segment: int
    segment_start_message: int
    segment_end_message: int
    role: str
    content: str
    messages: tuple[SessionLogEntryMessage, ...]


class SessionLogOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    requested_ref: str | None = None
    source: str | None = None
    total_entries: int
    total_segments: int
    segment_index: int | None = None
    segment_entries: int | None = None
    segment_label: str | None = None
    showing: str
    entries: tuple[SessionLogEntry, ...]
    previous_command: str | None = None
    next_command: str | None = None
    hints: tuple[str, ...] = ()

    @property
    def messages(self) -> tuple[SessionLogEntryMessage, ...]:
        return tuple(message for entry in self.entries for message in entry.messages)

    @property
    def total_messages(self) -> int:
        return self.total_entries

    @property
    def segment_messages(self) -> int | None:
        return self.segment_entries

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
        entry_label = "entry" if self.total_entries == 1 else "entries"
        lines = [
            (
                f"Session {session_label} — showing "
                f"{self.showing} of {self.total_entries} {entry_label}"
            )
        ]
        if self.segment_label is not None and self.segment_entries is not None:
            lines.append(f"{self.segment_label}; {self.segment_entries} entries in segment")
        for entry in self.entries:
            lines.append("")
            lines.append(
                f"--- {entry.index} [segment {entry.segment} · "
                f"messages {entry.segment_start_message}-{entry.segment_end_message}] "
                f"[{entry.role}] ---"
            )
            if not entry.messages:
                lines.append(entry.content)
                continue
            if len(entry.messages) == 1:
                lines.append(entry.messages[0].content)
                continue

            for message in entry.messages:
                lines.append("")
                lines.append(f"[message {message.segment_message} · {message.role}]")
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
    if uses_absolute_window or payload.full:
        return ()
    if payload.tail is None:
        return ("Use --full to show the entire selected segment.",)
    return ()


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


def _truncate_preview(content: str) -> str:
    if not content:
        return content

    line_parts = content.splitlines(keepends=True)
    limited_by_lines = len(line_parts) > _CONTENT_PREVIEW_MAX_LINES
    preview = (
        "".join(line_parts[:_CONTENT_PREVIEW_MAX_LINES]) if limited_by_lines else content
    )
    if len(preview) > _CONTENT_PREVIEW_MAX_CHARS:
        preview = preview[:_CONTENT_PREVIEW_MAX_CHARS]

    omitted_chars = len(content) - len(preview)
    if omitted_chars <= 0:
        return content

    omitted_lines = (
        len(line_parts) - _CONTENT_PREVIEW_MAX_LINES if limited_by_lines else 0
    )
    line_label = "line" if omitted_lines == 1 else "lines"
    char_label = "char" if omitted_chars == 1 else "chars"
    marker = (
        f"...[truncated: omitted {omitted_lines} {line_label}, "
        f"{omitted_chars} {char_label}; rerun with --no-truncate]"
    )
    if not preview:
        return marker
    separator = "" if preview.endswith("\n") else "\n"
    return f"{preview}{separator}{marker}"


def _entry_message_row(
    message: AbsoluteTranscriptMessage,
    *,
    truncate_content: bool,
) -> SessionLogEntryMessage:
    content = _truncate_preview(message.content) if truncate_content else message.content
    return SessionLogEntryMessage(
        segment_message=message.segment_message_index,
        role=message.role,
        content=content,
    )


def _entry_row_with_index(
    entry: AbsoluteTranscriptEntry,
    *,
    index: int,
    truncate_content: bool,
) -> SessionLogEntry:
    content = _truncate_preview(entry.content) if truncate_content else entry.content
    return SessionLogEntry(
        index=index,
        segment=entry.segment_index,
        segment_start_message=entry.start_segment_message_index,
        segment_end_message=entry.end_segment_message_index,
        role=entry.role,
        content=content,
        messages=tuple(
            _entry_message_row(message, truncate_content=truncate_content)
            for message in entry.messages
        ),
    )


def _entries_with_absolute_ordinals(
    entries: tuple[AbsoluteTranscriptEntry, ...],
) -> tuple[AbsoluteTranscriptEntry, ...]:
    return tuple(
        entry._replace(ordinal=entry.absolute_ordinal)
        for entry in entries
        if entry.absolute_ordinal is not None
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

    uses_window_selectors = any(absolute_selectors)
    selector_requests_segment_zero = (
        payload.from_ordinal == 0
        or payload.before_ordinal == 0
        or payload.around_ordinal == 0
    )
    uses_segment_local_selectors = uses_window_selectors and (
        payload.segment is not None or selector_requests_segment_zero
    )
    if uses_window_selectors:
        if payload.tail is not None:
            raise ValueError("--tail cannot be combined with --from/--before/--around.")
        if payload.full:
            raise ValueError("--full cannot be combined with --from/--before/--around.")
    elif payload.full and payload.tail is not None:
        raise ValueError("--full cannot be combined with --tail.")

    if payload.context is not None and payload.around_ordinal is None:
        raise ValueError("--context requires --around.")
    if payload.limit is not None and (
        payload.from_ordinal is None and payload.before_ordinal is None
    ):
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
    selected_segment_entries: int | None = None

    if not uses_window_selectors or uses_segment_local_selectors:
        selected_segment_index = _resolve_segment_index(
            total_segments=len(parsed.segments),
            segment=payload.segment,
        )
        selected_segment_label = _segment_label(
            selected_index=selected_segment_index,
            total_segments=len(parsed.segments),
        )
        segment_entries = list(parsed.segment_entries[selected_segment_index])
        selected_segment_entries = len(segment_entries)

        if not uses_window_selectors:
            interaction_entries = [
                entry for entry in segment_entries if entry.kind == "interaction"
            ]
            resolved_tail = (
                None if payload.full else (payload.tail if payload.tail is not None else 5)
            )
            page = window_from_tail(
                segment_entries if payload.full else interaction_entries,
                tail=resolved_tail,
                first_ordinal=0 if payload.full else 1,
            )
        elif payload.from_ordinal is not None:
            limit = payload.limit if payload.limit is not None else 0
            page = window_from_from_limit(
                segment_entries,
                start_ordinal=payload.from_ordinal,
                limit=limit,
                first_ordinal=0,
            )
        elif payload.before_ordinal is not None:
            limit = payload.limit if payload.limit is not None else 0
            page = window_from_before_limit(
                segment_entries,
                before_ordinal=payload.before_ordinal,
                limit=limit,
                first_ordinal=0,
            )
        else:
            around_ordinal = payload.around_ordinal if payload.around_ordinal is not None else 0
            context = payload.context if payload.context is not None else 0
            page = window_from_around_context(
                segment_entries,
                around_ordinal=around_ordinal,
                context=context,
                first_ordinal=0,
            )
    elif payload.from_ordinal is not None:
        limit = payload.limit if payload.limit is not None else 0
        absolute_entries = _entries_with_absolute_ordinals(parsed.entries)
        page = window_from_from_limit(
            absolute_entries,
            start_ordinal=payload.from_ordinal,
            limit=limit,
        )
    elif payload.before_ordinal is not None:
        limit = payload.limit if payload.limit is not None else 0
        absolute_entries = _entries_with_absolute_ordinals(parsed.entries)
        page = window_from_before_limit(
            absolute_entries,
            before_ordinal=payload.before_ordinal,
            limit=limit,
        )
    else:
        around_ordinal = payload.around_ordinal if payload.around_ordinal is not None else 1
        context = payload.context if payload.context is not None else 0
        absolute_entries = _entries_with_absolute_ordinals(parsed.entries)
        page = window_from_around_context(
            absolute_entries,
            around_ordinal=around_ordinal,
            context=context,
        )

    previous_command: str | None = None
    next_command: str | None = None
    if uses_window_selectors:
        nav_limit = (
            payload.limit
            if payload.limit is not None
            else (payload.context if payload.context is not None else 5) * 2 + 1
        )
        if page.previous_from is not None:
            previous_command = build_session_log_command(
                parsed.route,
                segment_index=selected_segment_index if uses_segment_local_selectors else None,
                from_ordinal=page.previous_from,
                limit=nav_limit,
            )
        if page.next_from is not None:
            next_command = build_session_log_command(
                parsed.route,
                segment_index=selected_segment_index if uses_segment_local_selectors else None,
                from_ordinal=page.next_from,
                limit=nav_limit,
            )

    use_global_display_index = uses_window_selectors and not uses_segment_local_selectors
    output_entries = tuple(
        _entry_row_with_index(
            item,
            index=(
                item.absolute_ordinal
                if use_global_display_index and item.absolute_ordinal
                else item.ordinal
            ),
            truncate_content=payload.truncate,
        )
        for item in page.messages
    )

    total_entries = (
        selected_segment_entries
        if selected_segment_entries is not None
        else len(parsed.entries)
    )

    return SessionLogOutput(
        session_id=parsed.target.session_id,
        requested_ref=payload.ref.strip() or None,
        source=parsed.target.source,
        total_entries=total_entries,
        total_segments=len(parsed.segments),
        segment_index=selected_segment_index,
        segment_entries=selected_segment_entries,
        segment_label=selected_segment_label,
        showing=showing_window(page.start_ordinal, page.end_ordinal),
        entries=output_entries,
        previous_command=previous_command,
        next_command=next_command,
        hints=_window_hints(payload, uses_absolute_window=uses_window_selectors),
    )


session_log = async_from_sync(session_log_sync)


__all__ = [
    "SessionLogEntry",
    "SessionLogEntryMessage",
    "SessionLogInput",
    "SessionLogOutput",
    "session_log",
    "session_log_sync",
]
