"""Cursor extractor tests."""

from __future__ import annotations

import json

from meridian.lib.core.types import ArtifactKey, SpawnId
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.extractors.cursor import CURSOR_EXTRACTOR


class _MemoryArtifactStore:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self._payloads = payloads

    def get(self, key: ArtifactKey) -> bytes:
        return self._payloads[str(key)]

    def exists(self, key: ArtifactKey) -> bool:
        return str(key) in self._payloads


def _artifact_store_from_lines(
    spawn_id: SpawnId,
    lines: list[dict[str, object]],
) -> _MemoryArtifactStore:
    encoded = "\n".join(json.dumps(line) for line in lines).encode("utf-8")
    return _MemoryArtifactStore({f"{spawn_id}/output.jsonl": encoded})


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

    usage = CURSOR_EXTRACTOR.extract_usage(store, spawn_id)

    assert CURSOR_EXTRACTOR.extract_session_id(store, spawn_id) == "ses-cursor-1"
    assert usage.input_tokens == 123
    assert usage.output_tokens == 45
    assert usage.cache_read_input_tokens == 7
    assert usage.cache_creation_input_tokens == 8
    assert CURSOR_EXTRACTOR.extract_report(store, spawn_id) == "final cursor report"


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

    assert CURSOR_EXTRACTOR.extract_report(store, spawn_id) is None
    assert CURSOR_EXTRACTOR.extract_session_id(store, spawn_id) is None


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

    assert CURSOR_EXTRACTOR.extract_report(store, spawn_id) == "final assistant reply"


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

    assert CURSOR_EXTRACTOR.extract_report(store, spawn_id) == "terminal result text"

