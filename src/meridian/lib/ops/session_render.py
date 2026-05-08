"""Session transcript rendering and pagination helpers."""

from __future__ import annotations

from typing import NamedTuple

from meridian.lib.harness.transcript import TranscriptMessage


class SessionPagination(NamedTuple):
    messages: list[TranscriptMessage]
    start_index: int
    has_newer: bool
    has_older: bool


def select_compaction_segment(
    segments: list[list[TranscriptMessage]],
    *,
    compaction: int,
) -> list[TranscriptMessage]:
    if compaction < 0:
        raise ValueError("compaction must be >= 0")

    segment_index = len(segments) - 1 - compaction
    if segment_index < 0:
        raise ValueError(
            f"Compaction segment {compaction} out of range (available: 0-{len(segments) - 1})"
        )
    return segments[segment_index]


def paginate_segment(
    messages: list[TranscriptMessage],
    *,
    last_n: int | None,
    offset: int,
) -> SessionPagination:
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if last_n is not None and last_n < 0:
        raise ValueError("last_n must be >= 0")

    total = len(messages)
    if offset >= total:
        return SessionPagination(
            messages=[],
            start_index=total,
            has_newer=offset > 0,
            has_older=total > 0,
        )

    end = total - offset
    start = 0 if last_n is None else max(end - last_n, 0)
    selected = messages[start:end]
    return SessionPagination(
        messages=selected,
        start_index=start,
        has_newer=offset > 0,
        has_older=start > 0,
    )


def showing_window(start_index: int, count: int) -> str:
    if count <= 0:
        return "0-0"
    return f"{start_index + 1}-{start_index + count}"


__all__ = [
    "SessionPagination",
    "paginate_segment",
    "select_compaction_segment",
    "showing_window",
]
