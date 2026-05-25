"""Session transcript windowing helpers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from meridian.lib.ops.session_transcript import AbsoluteTranscriptEntry


class SessionWindow(NamedTuple):
    messages: list[AbsoluteTranscriptEntry]
    total_messages: int
    start_ordinal: int
    end_ordinal: int
    has_previous: bool
    has_next: bool
    previous_from: int | None
    next_from: int | None


def _window_bounds(messages: list[AbsoluteTranscriptEntry]) -> tuple[int, int]:
    if not messages:
        return (0, 0)
    return (messages[0].ordinal, messages[-1].ordinal)


def _window_with_navigation(
    *,
    all_messages: Sequence[AbsoluteTranscriptEntry],
    selected: list[AbsoluteTranscriptEntry],
    first_ordinal: int = 1,
    nav_limit: int | None = None,
) -> SessionWindow:
    total_messages = len(all_messages)
    start_ordinal, end_ordinal = _window_bounds(selected)
    max_ordinal = first_ordinal + max(total_messages - 1, 0)
    has_previous = bool(selected) and start_ordinal > first_ordinal
    has_next = bool(selected) and end_ordinal < max_ordinal

    if selected:
        window_size = nav_limit if nav_limit is not None else len(selected)
    else:
        window_size = nav_limit if nav_limit is not None else 0

    previous_from: int | None = None
    next_from: int | None = None
    if has_previous and window_size > 0:
        previous_from = max(start_ordinal - window_size, first_ordinal)
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


def window_from_tail(
    messages: Sequence[AbsoluteTranscriptEntry],
    *,
    tail: int | None,
    first_ordinal: int = 1,
) -> SessionWindow:
    if tail is not None and tail < 0:
        raise ValueError("--tail must be >= 0.")

    selected = list(messages) if tail is None else (list(messages[-tail:]) if tail > 0 else [])
    return _window_with_navigation(
        all_messages=messages,
        selected=selected,
        first_ordinal=first_ordinal,
    )


def window_from_from_limit(
    messages: Sequence[AbsoluteTranscriptEntry],
    *,
    start_ordinal: int,
    limit: int,
    first_ordinal: int = 1,
) -> SessionWindow:
    if start_ordinal < first_ordinal:
        raise ValueError(f"--from expects an entry ordinal >= {first_ordinal}.")
    if limit < 0:
        raise ValueError("--limit must be >= 0.")
    start_index = start_ordinal - first_ordinal
    selected = list(messages[start_index : start_index + limit])
    return _window_with_navigation(
        all_messages=messages,
        selected=selected,
        first_ordinal=first_ordinal,
        nav_limit=limit,
    )


def window_from_before_limit(
    messages: Sequence[AbsoluteTranscriptEntry],
    *,
    before_ordinal: int,
    limit: int,
    first_ordinal: int = 1,
) -> SessionWindow:
    if before_ordinal < first_ordinal:
        raise ValueError(f"--before expects an entry ordinal >= {first_ordinal}.")
    if limit < 0:
        raise ValueError("--limit must be >= 0.")
    end_index = max(before_ordinal - first_ordinal, 0)
    start_index = max(end_index - limit, 0)
    selected = list(messages[start_index:end_index])
    return _window_with_navigation(
        all_messages=messages,
        selected=selected,
        first_ordinal=first_ordinal,
        nav_limit=limit,
    )


def window_from_around_context(
    messages: Sequence[AbsoluteTranscriptEntry],
    *,
    around_ordinal: int,
    context: int,
    first_ordinal: int = 1,
) -> SessionWindow:
    if context < 0:
        raise ValueError("--context must be >= 0.")
    if around_ordinal < first_ordinal:
        raise ValueError(f"--around expects an entry ordinal >= {first_ordinal}.")
    if not messages:
        raise ValueError("--around is out of range (transcript has no entries).")
    max_ordinal = first_ordinal + len(messages) - 1
    if around_ordinal > max_ordinal:
        raise ValueError(
            f"--around {around_ordinal} out of range "
            f"(transcript has entries {first_ordinal}-{max_ordinal})."
        )
    center_index = around_ordinal - first_ordinal
    start_index = max(center_index - context, 0)
    end_index = min(center_index + context + 1, len(messages))
    selected = list(messages[start_index:end_index])
    window_size = context * 2 + 1
    return _window_with_navigation(
        all_messages=messages,
        selected=selected,
        first_ordinal=first_ordinal,
        nav_limit=window_size,
    )


def showing_window(start_ordinal: int, end_ordinal: int) -> str:
    if start_ordinal < 0 or end_ordinal < 0 or end_ordinal < start_ordinal:
        return "0-0"
    return f"{start_ordinal}-{end_ordinal}"


__all__ = [
    "SessionWindow",
    "showing_window",
    "window_from_around_context",
    "window_from_before_limit",
    "window_from_from_limit",
    "window_from_tail",
]
