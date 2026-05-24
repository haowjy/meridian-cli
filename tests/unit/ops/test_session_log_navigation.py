"""Unit tests for deterministic session log navigation windows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.ops.session_log import SessionLogInput, session_log_sync


def _write_session_file(path: Path) -> None:
    lines = [
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "m1"}]}}),
        json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "m2"}]}}),
        json.dumps({"type": "system", "subtype": "compact_boundary"}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "m3"}]}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "m4"}]}}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_session_log_default_shows_full_current_segment(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    _write_session_file(session_file)

    output = session_log_sync(SessionLogInput(file_path=session_file.as_posix()))

    assert output.showing == "3-4"
    assert output.segment_index == 1
    assert [message.index for message in output.messages] == [3, 4]


def test_session_log_tail_and_absolute_window_are_deterministic(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    _write_session_file(session_file)

    tail_output = session_log_sync(SessionLogInput(file_path=session_file.as_posix(), tail=1))
    assert [message.index for message in tail_output.messages] == [4]

    around_output = session_log_sync(
        SessionLogInput(file_path=session_file.as_posix(), around_ordinal=2, context=1)
    )
    assert [message.index for message in around_output.messages] == [1, 2, 3]
    assert around_output.next_command is not None
    assert "--from 4 --limit 3" in around_output.next_command


def test_session_log_rejects_conflicting_selectors(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    _write_session_file(session_file)

    with pytest.raises(ValueError, match="cannot be combined"):
        session_log_sync(
            SessionLogInput(
                file_path=session_file.as_posix(),
                from_ordinal=2,
                limit=1,
                tail=1,
            )
        )
