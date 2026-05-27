"""Session log operation with deterministic transcript navigation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.util import FormatContext
from meridian.lib.harness.transcript import ToolCall
from meridian.lib.ops.runtime import async_from_sync
from meridian.lib.ops.session_log_render import render_session_log
from meridian.lib.ops.session_render import (
    SessionWindow,
    showing_window,
    window_from_around_context,
    window_from_before_limit,
    window_from_from_limit,
    window_from_tail,
)
from meridian.lib.ops.session_transcript import (
    AbsoluteTranscriptEntry,
    AbsoluteTranscriptMessage,
    SessionLogRoute,
    build_session_log_command,
    read_session_transcript,
)


class SessionLogInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str = ""
    segment: str | None = None
    global_scope: bool = False
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
    tool_call: ToolCall | None = None
    is_tool_result: bool = False


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
    previous_segment_command: str | None = None
    next_segment_command: str | None = None
    hints: tuple[str, ...] = ()
    truncate: bool = True

    @property
    def messages(self) -> tuple[SessionLogEntryMessage, ...]:
        return tuple(message for entry in self.entries for message in entry.messages)

    def format_text(self, ctx: FormatContext | None = None) -> str:
        resolved_ctx = ctx or FormatContext()
        return render_session_log(
            session_id=self.session_id,
            requested_ref=self.requested_ref,
            source=self.source,
            total_entries=self.total_entries,
            segment_entries=self.segment_entries,
            segment_label=self.segment_label,
            showing=self.showing,
            entries=self.entries,
            previous_command=self.previous_command,
            next_command=self.next_command,
            previous_segment_command=self.previous_segment_command,
            next_segment_command=self.next_segment_command,
            hints=self.hints,
            truncate=self.truncate,
            verbosity=resolved_ctx.verbosity,
        )


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


def _entry_message_row(message: AbsoluteTranscriptMessage) -> SessionLogEntryMessage:
    return SessionLogEntryMessage(
        segment_message=message.segment_message_index,
        role=message.role,
        content=message.content,
        tool_call=message.tool_call,
        is_tool_result=message.is_tool_result,
    )


def _entry_row_with_index(
    entry: AbsoluteTranscriptEntry,
    *,
    index: int,
) -> SessionLogEntry:
    return SessionLogEntry(
        index=index,
        segment=entry.segment_index,
        segment_start_message=entry.start_segment_message_index,
        segment_end_message=entry.end_segment_message_index,
        role=entry.role,
        content=entry.content,
        messages=tuple(
            _entry_message_row(message) for message in entry.messages
        ),
    )


class _NavigationAddressSpace(NamedTuple):
    mode: Literal["segment", "global"]
    entries: tuple[AbsoluteTranscriptEntry, ...]
    ordinal_getter: Callable[[AbsoluteTranscriptEntry], int]
    first_ordinal: int
    segment_index: int | None
    segment_label: str | None
    segment_entries: int | None


def _entry_local_ordinal(entry: AbsoluteTranscriptEntry) -> int:
    return entry.ordinal


def _entry_global_ordinal(entry: AbsoluteTranscriptEntry) -> int:
    return entry.global_ordinal


def _window_navigation_size(payload: SessionLogInput) -> int:
    if payload.limit is not None:
        return payload.limit
    if payload.context is not None:
        return payload.context * 2 + 1
    if payload.tail is not None:
        return payload.tail
    return 5


def _segment_boundary_commands(
    *,
    parsed_route: SessionLogRoute,
    payload: SessionLogInput,
    page: SessionWindow,
    address_space: _NavigationAddressSpace,
    total_segments: int,
    segment_entries_by_index: tuple[tuple[AbsoluteTranscriptEntry, ...], ...],
) -> tuple[str | None, str | None]:
    if address_space.mode != "segment" or address_space.segment_index is None:
        return (None, None)
    if not page.entries:
        return (None, None)

    segment_index = address_space.segment_index
    max_ordinal = (
        address_space.ordinal_getter(address_space.entries[-1])
        if address_space.entries
        else address_space.first_ordinal
    )
    touches_top = page.start_ordinal == address_space.first_ordinal
    touches_bottom = page.end_ordinal == max_ordinal
    window_size = _window_navigation_size(payload)

    previous_segment_command: str | None = None
    if touches_top and segment_index > 0:
        previous_segment_index = segment_index - 1
        previous_segment_total = len(segment_entries_by_index[previous_segment_index])
        previous_start = max(previous_segment_total - window_size, 0)
        previous_segment_command = build_session_log_command(
            parsed_route,
            segment_index=previous_segment_index,
            from_ordinal=previous_start,
            limit=window_size,
        )

    next_segment_command: str | None = None
    if touches_bottom and segment_index < total_segments - 1:
        next_segment_command = build_session_log_command(
            parsed_route,
            segment_index=segment_index + 1,
            from_ordinal=0,
            limit=window_size,
        )

    return (previous_segment_command, next_segment_command)


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
    if payload.global_scope and payload.segment is not None:
        raise ValueError("--global cannot be combined with --segment.")
    if payload.global_scope and not uses_window_selectors:
        raise ValueError("--global requires --from, --before, or --around.")
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

    address_space: _NavigationAddressSpace

    if payload.global_scope:
        address_space = _NavigationAddressSpace(
            mode="global",
            entries=parsed.all_entries,
            ordinal_getter=_entry_global_ordinal,
            first_ordinal=0,
            segment_index=None,
            segment_label=None,
            segment_entries=None,
        )
    else:
        selected_segment_index = _resolve_segment_index(
            total_segments=len(parsed.segments),
            segment=payload.segment,
        )
        selected_segment_label = _segment_label(
            selected_index=selected_segment_index,
            total_segments=len(parsed.segments),
        )
        selected_segment_entries = len(parsed.segment_entries[selected_segment_index])
        address_space = _NavigationAddressSpace(
            mode="segment",
            entries=parsed.segment_entries[selected_segment_index],
            ordinal_getter=_entry_local_ordinal,
            first_ordinal=0,
            segment_index=selected_segment_index,
            segment_label=selected_segment_label,
            segment_entries=selected_segment_entries,
        )

    resolved_tail: int | None = None
    if not uses_window_selectors:
        interaction_entries = [
            entry for entry in address_space.entries if entry.kind == "interaction"
        ]
        resolved_tail = None if payload.full else (payload.tail if payload.tail is not None else 5)
        page = window_from_tail(
            address_space.entries if payload.full else interaction_entries,
            tail=resolved_tail,
            first_ordinal=address_space.first_ordinal if payload.full else 1,
            ordinal_getter=address_space.ordinal_getter,
        )
    elif payload.from_ordinal is not None:
        limit = payload.limit if payload.limit is not None else 0
        page = window_from_from_limit(
            address_space.entries,
            start_ordinal=payload.from_ordinal,
            limit=limit,
            first_ordinal=address_space.first_ordinal,
            ordinal_getter=address_space.ordinal_getter,
        )
    elif payload.before_ordinal is not None:
        limit = payload.limit if payload.limit is not None else 0
        page = window_from_before_limit(
            address_space.entries,
            before_ordinal=payload.before_ordinal,
            limit=limit,
            first_ordinal=address_space.first_ordinal,
            ordinal_getter=address_space.ordinal_getter,
        )
    else:
        around_ordinal = (
            payload.around_ordinal
            if payload.around_ordinal is not None
            else address_space.first_ordinal
        )
        context = payload.context if payload.context is not None else 0
        page = window_from_around_context(
            address_space.entries,
            around_ordinal=around_ordinal,
            context=context,
            first_ordinal=address_space.first_ordinal,
            ordinal_getter=address_space.ordinal_getter,
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
                segment_index=address_space.segment_index,
                global_scope=payload.global_scope,
                from_ordinal=page.previous_from,
                limit=nav_limit,
            )
        if page.next_from is not None:
            next_command = build_session_log_command(
                parsed.route,
                segment_index=address_space.segment_index,
                global_scope=payload.global_scope,
                from_ordinal=page.next_from,
                limit=nav_limit,
            )
    elif page.has_previous:
        nav_limit = resolved_tail if resolved_tail is not None else 5
        if page.previous_from is not None:
            previous_command = build_session_log_command(
                parsed.route,
                segment_index=address_space.segment_index,
                from_ordinal=page.previous_from,
                limit=nav_limit,
            )

    previous_segment_command, next_segment_command = _segment_boundary_commands(
        parsed_route=parsed.route,
        payload=payload,
        page=page,
        address_space=address_space,
        total_segments=len(parsed.segments),
        segment_entries_by_index=parsed.segment_entries,
    )

    output_entries = tuple(
        _entry_row_with_index(
            item,
            index=address_space.ordinal_getter(item),
        )
        for item in page.entries
    )

    total_entries = (
        address_space.segment_entries
        if address_space.segment_entries is not None
        else len(parsed.all_entries)
    )

    return SessionLogOutput(
        session_id=parsed.target.session_id,
        requested_ref=payload.ref.strip() or None,
        source=parsed.target.source,
        total_entries=total_entries,
        total_segments=len(parsed.segments),
        segment_index=address_space.segment_index,
        segment_entries=address_space.segment_entries,
        segment_label=address_space.segment_label,
        showing=showing_window(page.start_ordinal, page.end_ordinal),
        entries=output_entries,
        previous_command=previous_command,
        next_command=next_command,
        previous_segment_command=previous_segment_command,
        next_segment_command=next_segment_command,
        hints=_window_hints(payload, uses_absolute_window=uses_window_selectors),
        truncate=payload.truncate,
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
