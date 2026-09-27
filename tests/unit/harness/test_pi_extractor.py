"""Pure Pi output-event extraction tests."""

from __future__ import annotations

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.attempt_facts import AttemptFacts
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.extractors.pi import PI_EXTRACTOR


def _output_store(spawn_id: SpawnId, lines: list[dict[str, object]]) -> AttemptFacts:
    fold = PI_EXTRACTOR.create_fold()
    facts = fold.facts
    for event in lines:
        _fold(fold, event)
    return facts


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

    usage = store.usage
    assert usage is not None

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

    assert store.final_text == "final\nreport"


def _fold(fold, payload):
    fold(
        RawHarnessEvent(
            harness_id="fixture",
            event_type=payload.get("type", payload.get("event_type", "")),
            payload=payload,
        )
    )
