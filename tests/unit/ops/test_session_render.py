from __future__ import annotations

import pytest

from meridian.lib.harness.transcript import TranscriptMessage
from meridian.lib.ops.session_render import (
    paginate_segment,
    select_compaction_segment,
    showing_window,
)


def _message(role: str, content: str) -> TranscriptMessage:
    return TranscriptMessage(role=role, content=content)


def test_select_compaction_segment_uses_latest_by_default() -> None:
    segments = [
        [_message("assistant", "first")],
        [_message("assistant", "second")],
    ]

    latest = select_compaction_segment(segments, compaction=0)
    older = select_compaction_segment(segments, compaction=1)

    assert [(item.role, item.content) for item in latest] == [("assistant", "second")]
    assert [(item.role, item.content) for item in older] == [("assistant", "first")]


def test_select_compaction_segment_validates_bounds() -> None:
    with pytest.raises(ValueError, match="compaction must be >= 0"):
        select_compaction_segment([[]], compaction=-1)

    with pytest.raises(ValueError, match="Compaction segment 2 out of range"):
        select_compaction_segment([[]], compaction=2)


def test_paginate_segment_builds_flags_without_side_effects() -> None:
    messages = [
        _message("assistant", "one"),
        _message("assistant", "two"),
        _message("assistant", "three"),
    ]

    page = paginate_segment(messages, last_n=2, offset=1)
    assert [(item.role, item.content) for item in page.messages] == [
        ("assistant", "one"),
        ("assistant", "two"),
    ]
    assert page.start_index == 0
    assert page.has_newer is True
    assert page.has_older is False


def test_paginate_segment_handles_empty_window_after_large_offset() -> None:
    empty = paginate_segment([_message("assistant", "only")], last_n=5, offset=99)
    assert empty.messages == []
    assert empty.start_index == 1
    assert empty.has_newer is True
    assert empty.has_older is True

    empty_from_empty = paginate_segment([], last_n=5, offset=0)
    assert empty_from_empty.messages == []
    assert empty_from_empty.start_index == 0
    assert empty_from_empty.has_older is False


def test_showing_window_uses_one_based_indexes() -> None:
    assert showing_window(0, 0) == "0-0"
    assert showing_window(0, 3) == "1-3"
    assert showing_window(2, 1) == "3-3"
