"""Pure Pi output-event extraction tests."""

from __future__ import annotations

import json

from meridian.lib.core.types import ArtifactKey, SpawnId
from meridian.lib.harness.extractors.pi import PI_EXTRACTOR


class _MemoryArtifactStore:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self._payloads = payloads

    def get(self, key: ArtifactKey) -> bytes:
        return self._payloads[str(key)]

    def exists(self, key: ArtifactKey) -> bool:
        return str(key) in self._payloads


def _output_store(spawn_id: SpawnId, events: list[dict[str, object]]) -> _MemoryArtifactStore:
    output = "\n".join(json.dumps(event) for event in events).encode()
    return _MemoryArtifactStore({f"{spawn_id}/output.jsonl": output})


def test_pi_extractor_reads_usage_from_latest_assistant_message_end() -> None:
    spawn_id = SpawnId("p-pi-usage")
    store = _output_store(
        spawn_id,
        [
            {"type": "message_end", "message": {"role": "user", "usage": {"input": 1}}},
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "usage": {
                        "input": 123,
                        "output": 45,
                        "cacheRead": 7,
                        "cacheWrite": 8,
                        "cost": {"total": 0.25},
                    },
                },
            },
        ],
    )

    usage = PI_EXTRACTOR.extract_usage(store, spawn_id)

    assert usage.input_tokens == 123
    assert usage.output_tokens == 45
    assert usage.cache_read_input_tokens == 7
    assert usage.cache_creation_input_tokens == 8
    assert usage.total_cost_usd == 0.25


def test_pi_extractor_reads_report_from_last_assistant_agent_end_message() -> None:
    spawn_id = SpawnId("p-pi-report")
    store = _output_store(
        spawn_id,
        [
            {
                "type": "agent_end",
                "messages": [
                    {"role": "assistant", "content": [{"type": "text", "text": "old"}]},
                    {"role": "user", "content": [{"type": "text", "text": "ignored"}]},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "hidden"},
                            {"type": "text", "text": "final"},
                            {"type": "text", "text": "report"},
                        ],
                    },
                ],
            }
        ],
    )

    assert PI_EXTRACTOR.extract_report(store, spawn_id) == "final\nreport"
