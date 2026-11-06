"""Malformed output must preserve good evidence without certifying partial usage."""



from meridian.lib.core.types import SpawnId
from meridian.lib.harness.extractors.claude import ClaudeFold, ClaudeHarnessExtractor
from meridian.lib.launch.extract import enrich_finalize
from tests.support.launch_paths import launch_paths


def test_stdout_bad_byte_and_fold_error_preserve_report(tmp_path):
    class DriftingFold(ClaudeFold):
        def fold_event(self, kind, payload):
            if kind == "broken":
                raise ValueError("format drift")
            super().fold_event(kind, payload)

    output = tmp_path / "output.jsonl"
    output.write_bytes(
        b'\xff\n{"type":"broken"}\n'
        b'{"type":"result","result":"surviving report",'
        b'"modelUsage":{"m":{"inputTokens":7}}}\n'
    )
    extractor = ClaudeHarnessExtractor()
    fold = DriftingFold(extractor)
    facts = fold.facts
    fold.fold_stdout(output, "claude")
    assert facts.incomplete
    assert facts.final_text == "surviving report"
    result = enrich_finalize(
        extractor=extractor,
        facts=facts,
        launch=launch_paths(tmp_path, SpawnId("p1")),
    )
    assert result.report_path.read_text().endswith("surviving report\n")
    assert result.usage is not None
    assert result.usage.input_tokens == 7
















