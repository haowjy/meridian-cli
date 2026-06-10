"""Unit tests for session search open-command rendering."""

from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.ops.session_search import SessionSearchInput, session_search_sync


def test_session_search_file_mode_emits_deterministic_open_command(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    session_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "alpha"}]},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "needle beta"}]},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    output = session_search_sync(
        SessionSearchInput(query="needle", file_path=session_file.as_posix())
    )

    assert len(output.matches) == 1
    assert output.matches[0].entry_ordinal == 1
    assert output.matches[0].open_command.endswith("--segment 0 --around 1 --context 5")


def test_session_search_open_command_is_explicit_for_non_current_segment(tmp_path: Path) -> None:
    session_file = tmp_path / "session-compacted.jsonl"
    session_file.write_text(
        "\n".join(
            [
                json.dumps({"type": "system", "content": "initial system prompt"}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "segment0"}]},
                    }
                ),
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "compact_boundary",
                        "summary": "handoff summary",
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "needle in segment1"}]},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    output = session_search_sync(
        SessionSearchInput(query="needle", file_path=session_file.as_posix())
    )

    assert len(output.matches) == 1
    assert output.matches[0].segment == 1
    assert output.matches[0].open_command.endswith("--segment 1 --around 1 --context 5")


def test_session_search_matches_real_entry_zero_setup_and_opens_from_zero(tmp_path: Path) -> None:
    session_file = tmp_path / "session-compacted.jsonl"
    session_file.write_text(
        "\n".join(
            [
                json.dumps({"type": "system", "content": "initial system prompt needle"}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "segment0"}]},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    output = session_search_sync(
        SessionSearchInput(query="needle", file_path=session_file.as_posix())
    )

    assert len(output.matches) == 1
    assert output.matches[0].entry_ordinal == 0
    assert output.matches[0].open_command.endswith("--segment 0 --from 0 --limit 1")


def test_session_search_does_not_match_entry_zero_placeholder(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    session_file.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "normal content"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    output = session_search_sync(
        SessionSearchInput(query="slot reserved", file_path=session_file.as_posix())
    )

    assert output.matches == ()


def test_session_search_consumed_follow_on_handoff_not_duplicated(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    session_file.write_text(
        "\n".join(
            [
                json.dumps({"type": "assistant", "message": {"content": "before"}}),
                json.dumps({"type": "system", "subtype": "compact_boundary"}),
                json.dumps(
                    {
                        "type": "user",
                        "isSynthetic": True,
                        "message": {"content": [{"type": "text", "text": "needle handoff"}]},
                    }
                ),
                json.dumps({"type": "assistant", "message": {"content": "after"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    output = session_search_sync(
        SessionSearchInput(query="needle", file_path=session_file.as_posix())
    )

    assert len(output.matches) == 1
    assert output.matches[0].segment == 1
    assert output.matches[0].entry_ordinal == 0
    assert output.matches[0].open_command.endswith("--segment 1 --from 0 --limit 1")

