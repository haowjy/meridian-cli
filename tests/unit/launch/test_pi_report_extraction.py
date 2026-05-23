"""Unit tests for Pi failure report extraction (issue #262)."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.extractors.pi import PI_EXTRACTOR
from meridian.lib.launch.constants import HISTORY_FILENAME
from meridian.lib.launch.errors import should_retry
from meridian.lib.launch.report import extract_or_fallback_report, extract_pi_failure_from_history
from meridian.lib.launch.streaming_runner import StreamingRunConclusion, _AttemptRuntime
from tests.unit.harness.test_extract_opencode_report import _MemoryArtifactStore

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "pi"


def _store_from_fixture(name: str, spawn_id: SpawnId) -> _MemoryArtifactStore:
    text = (_FIXTURES / name).read_text(encoding="utf-8")
    return _MemoryArtifactStore({f"{spawn_id}/{HISTORY_FILENAME}": text.encode("utf-8")})


def test_extract_pi_failure_from_shim_response_fixture() -> None:
    history = (_FIXTURES / "history_shim_response_failure.jsonl").read_text(encoding="utf-8")
    assert extract_pi_failure_from_history(history) == "No API key configured"


def test_extract_or_fallback_report_prefers_response_over_cleanup_fixture() -> None:
    spawn_id = SpawnId("p-pi-fixture-response")
    store = _store_from_fixture("history_shim_response_failure.jsonl", spawn_id)

    report = extract_or_fallback_report(store, spawn_id, extractor=PI_EXTRACTOR)

    assert report.content == "No API key configured"
    assert report.source == "assistant_message"
    assert "cleanup_completed" not in (report.content or "")


def test_extract_or_fallback_report_synthesizes_from_failure_reason_when_only_lifecycle() -> None:
    spawn_id = SpawnId("p-pi-fixture-broken")
    store = _store_from_fixture("history_broken_pi_lifecycle_only.jsonl", spawn_id)

    report = extract_or_fallback_report(
        store,
        spawn_id,
        extractor=PI_EXTRACTOR,
        failure_reason="pi_rpc_no_response_after_initial_prompt",
    )

    assert report.content == "pi_rpc_no_response_after_initial_prompt"
    assert report.source == "failure_reason"
    assert "cleanup_completed" not in (report.content or "")


def test_should_retry_skips_api_key_drain_error() -> None:
    assert not should_retry(
        exit_code=1,
        stderr="",
        failure_message="No API key configured",
        retries_attempted=0,
    )


def test_streaming_conclusion_sets_failure_reason_without_terminal_observed() -> None:
    conclusion = StreamingRunConclusion()
    attempt = _AttemptRuntime(
        connection=None,
        drain_exit_code=1,
        drain_error="No API key configured",
        timed_out=False,
        received_signal=None,
        budget_breach=None,
        terminated_by_report_watchdog=False,
        terminal_observed=False,
    )
    conclusion.absorb_attempt(attempt)
    if conclusion.exit_code != 0 and conclusion.failure_reason is None and attempt.drain_error:
        conclusion.failure_reason = attempt.drain_error

    facts = conclusion.terminal_facts(received_signal=None)
    assert facts.failure_reason == "No API key configured"
