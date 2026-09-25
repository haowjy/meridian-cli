"""Malformed output must preserve good evidence without certifying partial usage."""

import contextlib

import pytest
from structlog.testing import capture_logs

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.attempt_facts import AttemptFacts
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.extractors.claude import ClaudeFold, ClaudeHarnessExtractor
from meridian.lib.launch.extract import enrich_finalize
from meridian.lib.state.artifact_store import InMemoryStore


def test_stdout_bad_byte_and_fold_error_preserve_report(tmp_path):
    class DriftingFold(ClaudeFold):
        def fold_event(self, kind, payload):
            if kind == "broken":
                raise ValueError("format drift")
            super().fold_event(kind, payload)

    output = tmp_path / "output.jsonl"
    output.write_bytes(
        b'\xff\n{"type":"broken"}\n'
        b'{"type":"result","result":"surviving report","usage":{"input_tokens":7}}\n'
    )
    extractor = ClaudeHarnessExtractor()
    fold = DriftingFold(extractor)
    facts = fold.facts
    fold.fold_stdout(output)
    assert facts.incomplete
    assert facts.final_text == "surviving report"
    result = enrich_finalize(
        artifacts=InMemoryStore(),
        extractor=extractor,
        facts=facts,
        spawn_id=SpawnId("p1"),
        log_dir=tmp_path,
    )
    assert result.report_path.read_text().endswith("surviving report\n")
    assert result.usage is not None
    assert result.usage.input_tokens == 7


@pytest.mark.parametrize("incomplete", [False, True])
def test_finalize_logs_incomplete_facts_and_capped_report(tmp_path, incomplete):
    facts = AttemptFacts(incomplete=incomplete, usage=TokenUsage(input_tokens=7))
    facts.set_text("x" * (1024 * 1024 + 1))
    with capture_logs() as logs:
        result = enrich_finalize(
            artifacts=InMemoryStore(),
            extractor=ClaudeHarnessExtractor(),
            facts=facts,
            spawn_id=SpawnId("p1"),
            log_dir=tmp_path,
        )
    assert result.usage is not None
    assert any(row["event"] == "facts_incomplete" for row in logs) is incomplete
    assert "truncated" in result.report_path.read_text()


def test_unknown_usage_is_none_and_empty_failure_does_not_create_history(tmp_path):
    result = enrich_finalize(
        artifacts=InMemoryStore(),
        extractor=ClaudeHarnessExtractor(),
        facts=AttemptFacts(),
        spawn_id=SpawnId("p1"),
        log_dir=tmp_path,
        failure_reason="child exited before output",
    )
    assert result.usage is None
    assert result.report_path.is_file()
    assert not (tmp_path / "history.jsonl").exists()


def test_blank_stdout_lines_do_not_hide_known_usage(tmp_path):
    output = tmp_path / "output.jsonl"
    output.write_bytes(b'\n{"type":"result","result":"done","usage":{"input_tokens":7}}\n\n')
    extractor = ClaudeHarnessExtractor()
    fold = extractor.create_fold()
    fold.fold_stdout(output)
    result = enrich_finalize(
        artifacts=InMemoryStore(),
        extractor=extractor,
        facts=fold.facts,
        spawn_id=SpawnId("p1"),
        log_dir=tmp_path,
    )
    assert result.usage is not None
    assert result.usage.input_tokens == 7


def _finalize(tmp_path, extractor, facts):
    return enrich_finalize(
        artifacts=InMemoryStore(),
        extractor=extractor,
        facts=facts,
        spawn_id=SpawnId("p1"),
        log_dir=tmp_path,
    )


def test_non_json_stdout_line_keeps_harness_reported_cost(tmp_path):
    output = tmp_path / "output.jsonl"
    output.write_text(
        "Warning: something\n"
        '{"type":"result","result":"done","total_cost_usd":1.25,'
        '"usage":{"input_tokens":7,"output_tokens":3}}\n'
    )
    extractor = ClaudeHarnessExtractor()
    fold = extractor.create_fold()
    fold.fold_stdout(output)
    result = _finalize(tmp_path, extractor, fold.facts)
    assert result.usage is not None
    assert result.usage.total_cost_usd == 1.25
    assert result.usage.input_tokens == 7


class _DriftingClaudeFold(ClaudeFold):
    def fold_event(self, kind, payload):
        if kind == "broken":
            raise ValueError("format drift")
        super().fold_event(kind, payload)


def _feed(fold, *events):
    for event_type, payload in events:
        with contextlib.suppress(ValueError):
            fold(RawHarnessEvent(harness_id="claude", event_type=event_type, payload=payload))


def test_fold_failure_drops_generic_usage_but_keeps_later_harness_total(tmp_path):
    extractor = ClaudeHarnessExtractor()
    generic = _DriftingClaudeFold(extractor)
    _feed(
        generic,
        ("assistant", {"usage": {"input_tokens": 3}}),
        ("broken", {}),
        ("assistant", {"usage": {"input_tokens": 4, "output_tokens": 1}}),
    )
    assert generic.facts.incomplete
    assert _finalize(tmp_path, extractor, generic.facts).usage is None

    specific = _DriftingClaudeFold(extractor)
    _feed(
        specific,
        ("assistant", {"usage": {"input_tokens": 3}}),
        ("broken", {}),
        ("result", {"result": "done", "total_cost_usd": 1.25, "usage": {"input_tokens": 9}}),
    )
    usage = _finalize(tmp_path, extractor, specific.facts).usage
    assert usage is not None
    assert (usage.input_tokens, usage.total_cost_usd) == (9, 1.25)
