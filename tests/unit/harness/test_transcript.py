from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.harness.transcript import parse_transcript_events, parse_transcript_file
from meridian.lib.launch.constants import HISTORY_FILENAME, OUTPUT_FILENAME


def _write_jsonl(path: Path, *events: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def test_parse_transcript_file_uses_history_provider_for_seq_enveloped_events(
    tmp_path: Path,
) -> None:
    history_path = tmp_path / HISTORY_FILENAME
    _write_jsonl(
        history_path,
        {
            "seq": 0,
            "byte_offset": 0,
            "event_type": "assistant",
            "harness_id": "codex",
            "payload": {"message": {"content": [{"type": "text", "text": "from history"}]}},
        },
    )

    segments, total_compactions = parse_transcript_file(history_path)

    assert total_compactions == 0
    assert [(item.role, item.content) for item in segments[0]] == [("assistant", "from history")]


def test_parse_transcript_file_uses_jsonl_provider_for_legacy_output(tmp_path: Path) -> None:
    output_path = tmp_path / OUTPUT_FILENAME
    _write_jsonl(
        output_path,
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "from output"}]},
        },
    )

    segments, total_compactions = parse_transcript_file(output_path)

    assert total_compactions == 0
    assert [(item.role, item.content) for item in segments[0]] == [("assistant", "from output")]


def test_parse_transcript_events_splits_on_compaction_boundary() -> None:
    segments, total_compactions = parse_transcript_events(
        [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "before"}]},
            },
            {"type": "system", "subtype": "compact_boundary"},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "after"}]},
            },
        ]
    )

    assert total_compactions == 1
    assert [(item.role, item.content) for item in segments[0]] == [("assistant", "before")]
    assert [(item.role, item.content) for item in segments[1]] == [("assistant", "after")]
