"""Session transcript rendering and windowing helpers."""

from __future__ import annotations

from typing import NamedTuple

from meridian.lib.harness.transcript import TranscriptMessage


class AbsoluteTranscriptMessage(NamedTuple):
    ordinal: int
    segment_index: int
    segment_message_index: int
    role: str
    content: str


class SessionWindow(NamedTuple):
    messages: list[AbsoluteTranscriptMessage]
    total_messages: int
    start_ordinal: int
    end_ordinal: int
    has_previous: bool
    has_next: bool
    previous_from: int | None
    next_from: int | None


def flatten_transcript_segments(
    segments: list[list[TranscriptMessage]],
) -> list[AbsoluteTranscriptMessage]:
    flattened: list[AbsoluteTranscriptMessage] = []
    ordinal = 1
    for segment_index, messages in enumerate(segments):
        for segment_message_index, message in enumerate(messages, start=1):
            flattened.append(
                AbsoluteTranscriptMessage(
                    ordinal=ordinal,
                    segment_index=segment_index,
                    segment_message_index=segment_message_index,
                    role=message.role,
                    content=message.content,
                )
            )
            ordinal += 1
    return flattened


def resolve_segment_index(
    *,
    segments: list[list[TranscriptMessage]],
    segment: str | None,
    compaction: int | None,
) -> int:
    total_segments = len(segments)
    if total_segments <= 0:
        return 0
    if segment is not None and compaction is not None:
        raise ValueError("Use either --segment or --compaction, not both.")

    if compaction is not None:
        if compaction < 0:
            raise ValueError("compaction must be >= 0")
        segment_index = total_segments - 1 - compaction
        if segment_index < 0:
            raise ValueError(
                f"Compaction segment {compaction} out of range (available: 0-{total_segments - 1})"
            )
        return segment_index

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


def segment_label(*, selected_index: int, total_segments: int) -> str:
    if total_segments <= 0:
        return "segment 0"
    if selected_index == total_segments - 1:
        return f"segment {selected_index} (current)"
    if total_segments >= 2 and selected_index == total_segments - 2:
        return f"segment {selected_index} (previous)"
    return f"segment {selected_index}"


def _window_bounds(
    messages: list[AbsoluteTranscriptMessage],
) -> tuple[int, int]:
    if not messages:
        return (0, 0)
    return (messages[0].ordinal, messages[-1].ordinal)


def _window_with_navigation(
    *,
    all_messages: list[AbsoluteTranscriptMessage],
    selected: list[AbsoluteTranscriptMessage],
    nav_limit: int | None = None,
) -> SessionWindow:
    total_messages = len(all_messages)
    start_ordinal, end_ordinal = _window_bounds(selected)
    has_previous = start_ordinal > 1
    has_next = end_ordinal > 0 and end_ordinal < total_messages
    if selected:
        window_size = nav_limit if nav_limit is not None else len(selected)
    else:
        window_size = nav_limit if nav_limit is not None else 0

    previous_from: int | None = None
    next_from: int | None = None
    if has_previous and window_size > 0:
        previous_from = max(start_ordinal - window_size, 1)
    if has_next and window_size > 0:
        next_from = end_ordinal + 1

    return SessionWindow(
        messages=selected,
        total_messages=total_messages,
        start_ordinal=start_ordinal,
        end_ordinal=end_ordinal,
        has_previous=has_previous,
        has_next=has_next,
        previous_from=previous_from,
        next_from=next_from,
    )


def paginate_recent_messages(
    messages: list[AbsoluteTranscriptMessage],
    *,
    last_n: int | None,
    offset: int,
) -> SessionWindow:
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if last_n is not None and last_n < 0:
        raise ValueError("last_n must be >= 0")

    total = len(messages)
    if offset >= total:
        return SessionWindow(
            messages=[],
            total_messages=total,
            start_ordinal=0,
            end_ordinal=0,
            has_previous=total > 0,
            has_next=offset > 0,
            previous_from=None,
            next_from=None,
        )

    end_index = total - offset
    start_index = 0 if last_n is None else max(end_index - last_n, 0)
    selected = messages[start_index:end_index]
    return _window_with_navigation(all_messages=messages, selected=selected)


def window_from_from_limit(
    messages: list[AbsoluteTranscriptMessage],
    *,
    start_ordinal: int,
    limit: int,
) -> SessionWindow:
    if start_ordinal < 1:
        raise ValueError("--from expects a message ordinal >= 1.")
    if limit < 0:
        raise ValueError("--limit must be >= 0.")
    start_index = start_ordinal - 1
    selected = messages[start_index : start_index + limit]
    return _window_with_navigation(
        all_messages=messages,
        selected=selected,
        nav_limit=limit,
    )


def window_from_before_limit(
    messages: list[AbsoluteTranscriptMessage],
    *,
    before_ordinal: int,
    limit: int,
) -> SessionWindow:
    if before_ordinal < 1:
        raise ValueError("--before expects a message ordinal >= 1.")
    if limit < 0:
        raise ValueError("--limit must be >= 0.")
    end_index = max(before_ordinal - 1, 0)
    start_index = max(end_index - limit, 0)
    selected = messages[start_index:end_index]
    return _window_with_navigation(
        all_messages=messages,
        selected=selected,
        nav_limit=limit,
    )


def window_from_around_context(
    messages: list[AbsoluteTranscriptMessage],
    *,
    around_ordinal: int,
    context: int,
) -> SessionWindow:
    if context < 0:
        raise ValueError("--context must be >= 0.")
    if around_ordinal < 1:
        raise ValueError("--around expects a message ordinal >= 1.")
    if around_ordinal > len(messages):
        raise ValueError(
            f"--around {around_ordinal} out of range (transcript has {len(messages)} messages)."
        )
    start_index = max(around_ordinal - 1 - context, 0)
    end_index = min(around_ordinal + context, len(messages))
    selected = messages[start_index:end_index]
    window_size = context * 2 + 1
    return _window_with_navigation(
        all_messages=messages,
        selected=selected,
        nav_limit=window_size,
    )


def showing_window(start_ordinal: int, end_ordinal: int) -> str:
    if start_ordinal <= 0 or end_ordinal <= 0 or end_ordinal < start_ordinal:
        return "0-0"
    return f"{start_ordinal}-{end_ordinal}"


__all__ = [
    "AbsoluteTranscriptMessage",
    "SessionWindow",
    "flatten_transcript_segments",
    "paginate_recent_messages",
    "resolve_segment_index",
    "segment_label",
    "showing_window",
    "window_from_around_context",
    "window_from_before_limit",
    "window_from_from_limit",
]
