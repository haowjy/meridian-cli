"""Parser regressions for running-spawn query."""

from __future__ import annotations

import json

from meridian.lib.ops.spawn.query import extract_last_assistant_message


def test_extract_last_assistant_message_handles_markers_and_json_events() -> None:
    codex_banner_text = "\n".join(
        [
            "OpenAI Codex v0.107.0",
            "model=gpt-5.3-codex",
            "harness=codex",
            "provider=openai",
        ]
    )
    assert extract_last_assistant_message(codex_banner_text) is None

    marker_stream_text = "\n".join(
        [
            "OpenAI Codex v0.107.0",
            "model: gpt-5.3-codex",
            "codex",
            "First response line.",
            "Second response line.",
            "exec",
            "/bin/bash -lc 'echo ok'",
            "codex",
            "Final assistant reply.",
        ]
    )
    assert extract_last_assistant_message(marker_stream_text) == "Final assistant reply."

    json_event_text = "\n".join(
        [
            json.dumps({"type": "assistant", "text": "json assistant message"}),
            "exec",
        ]
    )
    assert extract_last_assistant_message(json_event_text) == "json assistant message"
