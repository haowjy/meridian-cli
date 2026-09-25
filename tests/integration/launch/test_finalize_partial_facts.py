"""Malformed output must preserve good evidence without certifying partial usage."""

import pytest
from structlog.testing import capture_logs

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.attempt_facts import AttemptFacts
from meridian.lib.harness.extractors.claude import ClaudeHarnessExtractor
from meridian.lib.launch.extract import enrich_finalize
from meridian.lib.state.artifact_store import InMemoryStore


def test_stdout_bad_byte_and_fold_error_preserve_report(tmp_path):
    class DriftingExtractor(ClaudeHarnessExtractor):
        def fold(self, facts, event):
            if event.get("type") == "broken":
                raise ValueError("format drift")
            return super().fold(facts, event)

    output = tmp_path / "output.jsonl"
    output.write_bytes(
        b'\xff\n{"type":"broken"}\n'
        b'{"type":"result","result":"surviving report","usage":{"input_tokens":7}}\n'
    )
    facts = AttemptFacts()
    extractor = DriftingExtractor()
    facts.fold_stdout(extractor, output)
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
    assert result.usage is None


@pytest.mark.parametrize("incomplete", [False, True])
def test_finalize_exposes_partial_usage_and_capped_report(tmp_path, incomplete):
    facts = AttemptFacts(incomplete=incomplete, usage=TokenUsage(input_tokens=7))
    facts.set_text("x" * (1024 * 1024 + 1), "claude_result")
    with capture_logs() as logs:
        result = enrich_finalize(
            artifacts=InMemoryStore(),
            extractor=ClaudeHarnessExtractor(),
            facts=facts,
            spawn_id=SpawnId("p1"),
            log_dir=tmp_path,
        )
    assert (result.usage is None) is incomplete
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
