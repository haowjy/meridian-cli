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


def window_from_tail(
    messages: Sequence[AbsoluteTranscriptEntry],
    *,
    tail: int | None,
) -> SessionWindow:
    if tail is not None and tail < 0:
        raise ValueError("--tail must be >= 0.")

    selected = list(messages) if tail is None else (list(messages[-tail:]) if tail > 0 else [])
    return _window_with_navigation(all_messages=messages, selected=selected)


def window_from_from_limit(
    messages: Sequence[AbsoluteTranscriptEntry],
    *,
    start_ordinal: int,
    limit: int,
) -> SessionWindow:
    if start_ordinal < 1:
        raise ValueError("--from expects an entry ordinal >= 1.")
    if limit < 0:
        raise ValueError("--limit must be >= 0.")
    start_index = start_ordinal - 1
    selected = list(messages[start_index : start_index + limit])
    return _window_with_navigation(
        all_messages=messages,
        selected=selected,
        nav_limit=limit,
    )


def window_from_before_limit(
    messages: Sequence[AbsoluteTranscriptEntry],
    *,
    before_ordinal: int,
    limit: int,
) -> SessionWindow:
    if before_ordinal < 1:
        raise ValueError("--before expects an entry ordinal >= 1.")
    if limit < 0:
        raise ValueError("--limit must be >= 0.")
    end_index = max(before_ordinal - 1, 0)
    start_index = max(end_index - limit, 0)
    selected = list(messages[start_index:end_index])
    return _window_with_navigation(
        all_messages=messages,
        selected=selected,
        nav_limit=limit,
    )


def window_from_around_context(
    messages: Sequence[AbsoluteTranscriptEntry],
    *,
    around_ordinal: int,
    context: int,
) -> SessionWindow:
    if context < 0:
        raise ValueError("--context must be >= 0.")
    if around_ordinal < 1:
        raise ValueError("--around expects an entry ordinal >= 1.")
    if around_ordinal > len(messages):
        raise ValueError(
            f"--around {around_ordinal} out of range (transcript has {len(messages)} entries)."
        )
    start_index = max(around_ordinal - 1 - context, 0)
    end_index = min(around_ordinal + context, len(messages))
    selected = list(messages[start_index:end_index])
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
    "SessionWindow",
    "showing_window",
    "window_from_around_context",
    "window_from_before_limit",
    "window_from_from_limit",
    "window_from_tail",
]
