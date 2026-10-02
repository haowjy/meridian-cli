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


def test_pi_usage_counts_every_model_call_once_and_preserves_unknown_totals() -> None:
    messages = [
        {"role": "assistant", "usage": {"input": 100, "output": 20, "cost": {"total": 1.25}}},
        {"role": "assistant", "usage": {"input": 80, "output": 10, "cost": {"total": 0.50}}},
    ]
    fold = PI_EXTRACTOR.create_fold()
    for message in messages:
        _fold(fold, {"type": "message_end", "message": message})
    _fold(fold, {"type": "agent_end", "messages": messages})
    assert fold.facts.usage is not None
    assert fold.facts.usage.input_tokens == 180
    assert fold.facts.usage.output_tokens == 30
    assert fold.facts.usage.total_cost_usd == 1.75
    assert fold.facts.usage.cache_read_input_tokens is None
    _fold(fold, {"type": "message_end", "message": {"role": "assistant", "usage": {"output": 0}}})
    assert fold.facts.usage.input_tokens is None
    assert fold.facts.usage.output_tokens == 30
    assert fold.facts.usage.total_cost_usd is None


def _fold(fold, payload):
    fold(
        RawHarnessEvent(
            harness_id="fixture",
            event_type=payload.get("type", payload.get("event_type", "")),
            payload=payload,
        )
    )
