"""Unit tests for session search open-command rendering."""

from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.ops import session_transcript
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
    assert output.matches[0].open_command.endswith("--around 1 --context 5")


def test_session_search_single_target_parses_transcript_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_file = tmp_path / "session.jsonl"
    session_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "needle one"}]},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "needle two"}]},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    calls = {"count": 0}
    original_parse = session_transcript.parse_transcript_file

    def counting_parse(path: Path):
        calls["count"] += 1
        return original_parse(path)

    monkeypatch.setattr(session_transcript, "parse_transcript_file", counting_parse)

    output = session_search_sync(
        SessionSearchInput(query="needle", file_path=session_file.as_posix())
    )

    assert len(output.matches) == 1
    assert calls["count"] == 1
