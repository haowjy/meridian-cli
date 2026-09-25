"""Cursor extractor tests."""

from __future__ import annotations

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.attempt_facts import AttemptFacts
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.extractors.cursor import CURSOR_EXTRACTOR


def _artifact_store_from_lines(spawn_id: SpawnId, lines: list[dict[str, object]]) -> AttemptFacts:
    facts = AttemptFacts()
    for event in lines:
        CURSOR_EXTRACTOR.fold(facts, event)
    return facts


def test_cursor_extractor_reads_session_usage_and_result_report() -> None:
    spawn_id = SpawnId("p-cursor-extractor")
    store = _artifact_store_from_lines(
        spawn_id,
        [
            {
                "type": "system",
                "session_id": "ses-cursor-1",
            },
            {
                "type": "result",
                "usage": {
                    "inputTokens": 123,
                    "outputTokens": 45,
                    "cacheReadTokens": 7,
                    "cacheWriteTokens": 8,
                },
                "result": "final cursor report",
            },
        ],
    )

    usage = store.usage
    assert usage is not None

    assert store.first_session_id == "ses-cursor-1"
    assert usage.input_tokens == 123
    assert usage.output_tokens == 45
    assert usage.cache_read_input_tokens == 7
    assert usage.cache_creation_input_tokens == 8
    assert store.final_text == "final cursor report"


def test_cursor_extractor_detects_session_from_nested_event_payload() -> None:
    event = RawHarnessEvent(
        event_type="system",
        harness_id="cursor",
        payload={"payload": {"session": "ses-cursor-nested"}},
    )

    assert CURSOR_EXTRACTOR.detect_session_id_from_event(event) == "ses-cursor-nested"


def test_cursor_extractor_returns_none_when_report_or_session_missing() -> None:
    spawn_id = SpawnId("p-cursor-empty-report")
    store = _artifact_store_from_lines(
        spawn_id,
        [
            {
                "type": "result",
                "usage": {
                    "inputTokens": 1,
                    "outputTokens": 2,
                },
            }
        ],
    )

    assert store.final_text is None
    assert store.first_session_id is None


def test_cursor_extractor_falls_back_to_last_assistant_message_when_no_result() -> None:
    spawn_id = SpawnId("p-cursor-assistant-fallback")
    store = _artifact_store_from_lines(
        spawn_id,
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "first assistant reply"}],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "final assistant reply"}],
                },
            },
        ],
    )

    assert store.final_text == "final assistant reply"


def test_cursor_extractor_prefers_result_over_assistant_message() -> None:
    spawn_id = SpawnId("p-cursor-result-preferred")
    store = _artifact_store_from_lines(
        spawn_id,
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "assistant-only text"}],
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "result": "terminal result text",
            },
        ],
    )

    assert store.final_text == "terminal result text"
