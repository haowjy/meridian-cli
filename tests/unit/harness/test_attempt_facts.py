"""Attempt-local facts must not require any runner artifact."""

from meridian.lib.harness.attempt_facts import AttemptFacts
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.extractors.claude import CLAUDE_EXTRACTOR


def test_attempt_facts_reset_and_unknown_usage():
    fold = CLAUDE_EXTRACTOR.create_fold()
    first = fold.facts
    _fold(fold, {"type": "result", "session_id": "first", "result": "done"})
    assert first.final_text == "done"
    assert first.first_session_id == "first"
    assert first.usage is None
    second = AttemptFacts()
    assert second.first_session_id is None
    assert second.final_text is None
    assert second.usage is None
    assert not second.output_seen


def test_known_zero_is_not_unknown():
    from meridian.lib.harness.extractors.codex import CODEX_EXTRACTOR

    fold = CODEX_EXTRACTOR.create_fold()
    facts = fold.facts
    _fold(
        fold,
        {
            "type": "thread/tokenUsage/updated",
            "tokenUsage": {"total": {"inputTokens": 0, "reasoningOutputTokens": 0}},
        },
    )
    assert facts.usage is not None
    assert facts.usage.input_tokens == 0
    assert facts.usage.reasoning_tokens == 0
    assert facts.usage.output_tokens is None


def test_utf8_report_is_bounded_and_flagged():
    fold = CLAUDE_EXTRACTOR.create_fold()
    facts = fold.facts
    _fold(fold, {"type": "result", "result": "語" * 500_000})
    assert facts.text_capped
    assert facts.final_text is not None
    assert len(facts.final_text.encode("utf-8")) <= 1024 * 1024
    assert not facts.final_text.endswith("\ufffd")


def test_codex_child_report_and_work_after_message_cannot_win():
    from meridian.lib.harness.extractors.codex import CODEX_EXTRACTOR

    fold = CODEX_EXTRACTOR.create_fold()
    facts = fold.facts
    for event in [
        {"type": "turn/started", "threadId": "parent"},
        {
            "type": "item/completed",
            "threadId": "parent",
            "item": {"type": "agentMessage", "text": "parent report"},
        },
        {
            "type": "item/completed",
            "threadId": "child",
            "item": {"type": "agentMessage", "text": "child report"},
        },
    ]:
        _fold(fold, event)
    assert facts.final_text == "parent report"
    _fold(
        fold, {"type": "item/started", "threadId": "parent", "item": {"type": "commandExecution"}}
    )
    assert facts.final_text is None


def _fold(fold, payload):
    fold(
        RawHarnessEvent(
            harness_id="fixture",
            event_type=payload.get("type", payload.get("event_type", "")),
            payload=payload,
        )
    )


def test_raw_event_kind_wins_without_rewriting_payload():
    fold = CLAUDE_EXTRACTOR.create_fold()
    payload = {"event_type": "assistant", "result": "owned result"}
    fold(RawHarnessEvent(harness_id="claude", event_type="result", payload=payload))
    assert fold.facts.final_text == "owned result"
    assert payload == {"event_type": "assistant", "result": "owned result"}
