from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable, Sequence
from typing import NamedTuple

from meridian.lib.ops.session_transcript import AbsoluteTranscriptEntry


class SessionWindow(NamedTuple):
    entries: list[AbsoluteTranscriptEntry]
    total_entries: int
    start_ordinal: int
    end_ordinal: int
    has_previous: bool
    has_next: bool
    previous_from: int | None
    next_from: int | None


def _ordinals(
    entries: Sequence[AbsoluteTranscriptEntry],
    ordinal_getter: Callable[[AbsoluteTranscriptEntry], int],
) -> list[int]:
    return [ordinal_getter(entry) for entry in entries]


def _window_at(
    entries: Sequence[AbsoluteTranscriptEntry],
    *,
    start: int,
    end: int,
    ordinal_getter: Callable[[AbsoluteTranscriptEntry], int],
    nav_limit: int | None = None,
) -> SessionWindow:
    """Entries ``[start, end)`` of ``entries``, which hold only what the view shows.

    Ordinals are the entries' own, so hidden entries leave gaps and every position printed
    here is one any other view accepts.
    """
    selected = list(entries[start:end])
    start_ordinal = ordinal_getter(selected[0]) if selected else 0
    end_ordinal = ordinal_getter(selected[-1]) if selected else 0
    has_previous = bool(selected) and start > 0
    has_next = bool(selected) and end < len(entries)
    window_size = nav_limit if nav_limit is not None else len(selected)

    previous_from: int | None = None
    next_from: int | None = None
    if has_previous and window_size > 0:
        previous_from = ordinal_getter(entries[max(start - window_size, 0)])
    if has_next and window_size > 0:
        next_from = end_ordinal + 1

    return SessionWindow(
        entries=selected,
        total_entries=len(entries),
        start_ordinal=start_ordinal,
        end_ordinal=end_ordinal,
        has_previous=has_previous,
        has_next=has_next,
        previous_from=previous_from,
        next_from=next_from,
    )


def window_from_tail(
    entries: Sequence[AbsoluteTranscriptEntry],
    *,
    tail: int | None,
    ordinal_getter: Callable[[AbsoluteTranscriptEntry], int] = lambda entry: entry.ordinal,
) -> SessionWindow:
    if tail is not None and tail < 0:
        raise ValueError("--tail must be >= 0.")
    start = 0 if tail is None else (max(len(entries) - tail, 0) if tail > 0 else len(entries))
    return _window_at(entries, start=start, end=len(entries), ordinal_getter=ordinal_getter)


def window_from_from_limit(
    entries: Sequence[AbsoluteTranscriptEntry],
    *,
    start_ordinal: int,
    limit: int,
    first_ordinal: int = 1,
    ordinal_getter: Callable[[AbsoluteTranscriptEntry], int] = lambda entry: entry.ordinal,
) -> SessionWindow:
    if start_ordinal < first_ordinal:
        raise ValueError(f"--from expects an entry ordinal >= {first_ordinal}.")
    if limit < 0:
        raise ValueError("--limit must be >= 0.")
    start = bisect_left(_ordinals(entries, ordinal_getter), start_ordinal)
    return _window_at(
        entries, start=start, end=start + limit, ordinal_getter=ordinal_getter, nav_limit=limit
    )


def window_from_before_limit(
    entries: Sequence[AbsoluteTranscriptEntry],
    *,
    before_ordinal: int,
    limit: int,
    first_ordinal: int = 1,
    ordinal_getter: Callable[[AbsoluteTranscriptEntry], int] = lambda entry: entry.ordinal,
) -> SessionWindow:
    if before_ordinal < first_ordinal:
        raise ValueError(f"--before expects an entry ordinal >= {first_ordinal}.")
    if limit < 0:
        raise ValueError("--limit must be >= 0.")
    end = bisect_left(_ordinals(entries, ordinal_getter), before_ordinal)
    return _window_at(
        entries, start=max(end - limit, 0), end=end, ordinal_getter=ordinal_getter, nav_limit=limit
    )


def window_from_around_context(
    entries: Sequence[AbsoluteTranscriptEntry],
    *,
    around_ordinal: int,
    context: int,
    first_ordinal: int = 1,
    last_ordinal: int,
    ordinal_getter: Callable[[AbsoluteTranscriptEntry], int] = lambda entry: entry.ordinal,
) -> SessionWindow:
    """``context`` shown entries each side; ``last_ordinal`` bounds the whole transcript."""
    if context < 0:
        raise ValueError("--context must be >= 0.")
    if around_ordinal < first_ordinal:
        raise ValueError(f"--around expects an entry ordinal >= {first_ordinal}.")
    if last_ordinal < first_ordinal:
        raise ValueError("--around is out of range (transcript has no entries).")
    if around_ordinal > last_ordinal:
        raise ValueError(
            f"--around {around_ordinal} out of range "
            f"(transcript has entries {first_ordinal}-{last_ordinal})."
        )
    ordinals = _ordinals(entries, ordinal_getter)
    center = bisect_left(ordinals, around_ordinal)
    # A hidden center entry leaves a gap: take ``context`` shown entries on each side of it.
    shown_center = center < len(ordinals) and ordinals[center] == around_ordinal
    return _window_at(
        entries,
        start=max(center - context, 0),
        end=center + context + (1 if shown_center else 0),
        ordinal_getter=ordinal_getter,
        nav_limit=context * 2 + 1,
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
