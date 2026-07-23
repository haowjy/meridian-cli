"""Pure session transcript segment and navigation contracts."""

from __future__ import annotations

from typing import Any

import pytest

from meridian.lib.harness.transcript import parse_transcript_events_with_prologues
from meridian.lib.ops import session_log, session_render
from meridian.lib.ops.session_log import SessionLogEntry, SessionLogEntryMessage, SessionLogOutput
from meridian.lib.ops.session_transcript import (
    AbsoluteTranscriptEntry,
    build_segment_entries,
    flatten_transcript_segments,
    group_transcript_entries,
)


def _event(role: str, text: str) -> dict[str, Any]:
    return {"type": role, "message": {"content": [{"type": "text", "text": text}]}}


def _entries(
    *, handoff: str | None = "handoff summary"
) -> tuple[tuple[AbsoluteTranscriptEntry, ...], ...]:
    boundary: dict[str, Any] = {"type": "system", "subtype": "compact_boundary"}
    if handoff is not None:
        boundary["summary"] = handoff
    parsed = parse_transcript_events_with_prologues(
        [
            {"type": "system", "content": "initial prompt"},
            _event("user", "s0-u1"),
            _event("assistant", "s0-a1"),
            boundary,
            _event("user", "s1-u1"),
            _event("assistant", "s1-a1"),
        ]
    )
    messages = flatten_transcript_segments(parsed.segments)
    return build_segment_entries(
        segments=parsed.segments,
        segment_setups=parsed.segment_setups,
        interaction_entries=group_transcript_entries(messages),
    )


def test_segment_selection_resolves_current_previous_and_explicit_index() -> None:
    resolve = session_log._resolve_segment_index

    assert resolve(total_segments=3, segment=None) == 2
    assert resolve(total_segments=3, segment="previous") == 1
    assert resolve(total_segments=3, segment="0") == 0
    with pytest.raises(ValueError, match="out of range"):
        resolve(total_segments=3, segment="3")


def test_local_windows_preserve_segment_ordinals_and_navigation() -> None:
    current = _entries()[1]

    tail = session_render.window_from_tail(current, tail=2, first_ordinal=0)
    page = session_render.window_from_from_limit(
        current, start_ordinal=0, limit=2, first_ordinal=0
    )
    before = session_render.window_from_before_limit(
        current, before_ordinal=2, limit=1, first_ordinal=0
    )

    assert [entry.ordinal for entry in tail.entries] == [1, 2]
    assert tail.previous_from == 0
    assert [entry.ordinal for entry in page.entries] == [0, 1]
    assert page.next_from == 2
    assert [entry.ordinal for entry in before.entries] == [1]


def test_global_window_uses_global_ordinals_across_segments() -> None:
    all_entries = tuple(entry for segment in _entries() for entry in segment)
    page = session_render.window_from_around_context(
        all_entries,
        around_ordinal=3,
        context=1,
        first_ordinal=0,
        ordinal_getter=lambda entry: entry.global_ordinal,
    )

    assert [entry.global_ordinal for entry in page.entries] == [2, 3, 4]
    assert [(entry.segment_index, entry.content) for entry in page.entries] == [
        (0, "s0-a1"),
        (1, "handoff summary"),
        (1, "s1-u1"),
    ]


def test_decoded_handoff_becomes_the_next_segment_setup_slot() -> None:
    segments = _entries()

    assert segments[0][0].content == "initial prompt"
    assert segments[1][0].ordinal == 0
    assert segments[1][0].global_ordinal == 3
    assert segments[1][0].content == "handoff summary"
    assert segments[1][0].is_placeholder is False


def test_missing_handoff_reserves_the_segment_boundary_slot() -> None:
    current = _entries(handoff=None)[1]

    assert [entry.ordinal for entry in current] == [0, 1, 2]
    assert current[0].is_placeholder is True
    assert current[0].content.startswith("[compaction handoff slot reserved:")


def test_window_validation_rejects_invalid_ordinals_and_limits() -> None:
    current = _entries()[1]

    with pytest.raises(ValueError, match="--tail must be >= 0"):
        session_render.window_from_tail(current, tail=-1, first_ordinal=0)
    with pytest.raises(ValueError, match="--limit must be >= 0"):
        session_render.window_from_from_limit(
            current, start_ordinal=0, limit=-1, first_ordinal=0
        )
    with pytest.raises(ValueError, match="out of range"):
        session_render.window_from_around_context(
            current, around_ordinal=4, context=0, first_ordinal=0
        )


def test_format_text_truncates_oversized_in_memory_entry() -> None:
    large_text = "\n".join(f"line {index}" for index in range(1, 121))
    output = SessionLogOutput(
        session_id="session-1",
        source="synthetic transcript",
        total_entries=1,
        total_segments=1,
        showing="1-1",
        entries=(
            SessionLogEntry(
                index=1,
                segment=0,
                segment_start_message=1,
                segment_end_message=1,
                role="assistant",
                content=large_text,
                messages=(
                    SessionLogEntryMessage(
                        segment_message=1,
                        role="assistant",
                        content=large_text,
                    ),
                ),
            ),
        ),
    )

    assert output.entries[0].content == large_text
    assert "truncated:" in output.format_text()
    assert "rerun with --no-truncate" in output.format_text()
