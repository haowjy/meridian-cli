# qa-validated: pi-rpc-quiescence
import signal

from meridian.lib.core.domain import TokenUsage
from meridian.lib.launch.extract import (
    FinalizeExtraction,
    classify_finalize_report,
)
from meridian.lib.launch.report import ExtractedReport, ReportSource
from meridian.lib.launch.streaming_runner import (
    StreamingRunConclusion,
    _AttemptRuntime,
    _inactivity_terminal_outcome,
)


def _attempt(
    *,
    exit_code: int,
    watchdog: bool = False,
    terminal_observed: bool = False,
) -> _AttemptRuntime:
    return _AttemptRuntime(
        connection=None,
        drain_exit_code=exit_code,
        drain_error=None,
        timed_out=False,
        received_signal=None,
        budget_breach=None,
        terminated_by_report_watchdog=watchdog,
        terminal_observed=terminal_observed,
    )


def _extraction_with_report(
    report_text: str | None,
    *,
    source: ReportSource | None = "report_md",
) -> FinalizeExtraction:
    return FinalizeExtraction(
        usage=TokenUsage(total_cost_usd=1.25, input_tokens=10, output_tokens=20),
        harness_session_id=None,
        report_path=None,
        report=ExtractedReport(content=report_text, source=source),
        output_is_empty=False,
        report_kind=classify_finalize_report(ExtractedReport(content=report_text, source=source)),
    )


def test_absorb_attempt_updates_exit_code_and_terminal_flags() -> None:
    conclusion = StreamingRunConclusion()

    conclusion.absorb_attempt(_attempt(exit_code=7, terminal_observed=True))

    assert conclusion.exit_code == 7
    assert conclusion.final_attempt_terminal_observed is True


def test_terminal_facts_treat_durable_report_watchdog_as_success() -> None:
    conclusion = StreamingRunConclusion(
        exit_code=1,
        failure_reason="report_watchdog",
        extracted=_extraction_with_report("done"),
    )

    facts = conclusion.terminal_facts(received_signal=None)
    assert facts.exit_code == 1
    assert facts.failure_reason == "report_watchdog"
    assert facts.durable_report_completion is True
    assert facts.cancellation_observed is False


def test_terminal_facts_do_not_treat_synthetic_failure_report_as_completion() -> None:
    conclusion = StreamingRunConclusion(
        exit_code=130,
        failure_reason="cancelled",
        extracted=_extraction_with_report(
            "Cursor subprocess exited with code 130.",
            source="failure_reason",
        ),
        cancellation_observed=True,
    )

    facts = conclusion.terminal_facts(received_signal=None)
    assert facts.durable_report_completion is False
    assert facts.cancellation_observed is True


def test_terminal_facts_treat_signal_without_terminal_as_cancelled() -> None:
    conclusion = StreamingRunConclusion(exit_code=0, failure_reason=None)

    facts = conclusion.terminal_facts(received_signal=signal.SIGINT)
    assert facts.exit_code == 0
    assert facts.failure_reason is None
    assert facts.cancellation_observed is True


def test_retry_count_tracks_attempts() -> None:
    conclusion = StreamingRunConclusion()

    conclusion.retries_attempted += 1
    conclusion.retries_attempted += 1

    assert conclusion.retries_attempted == 2


def test_inactivity_terminal_outcome_success_when_durable_report_recovered() -> None:
    exit_override, failure_reason = _inactivity_terminal_outcome(
        _extraction_with_report("recovered report"),
    )

    assert exit_override == 0
    assert failure_reason is None


def test_inactivity_terminal_outcome_stalled_without_durable_report() -> None:
    exit_override, failure_reason = _inactivity_terminal_outcome(
        _extraction_with_report(None),
    )

    assert exit_override is None
    assert failure_reason == "stalled"


def test_inactivity_terminal_outcome_applied_to_conclusion_without_retry() -> None:
    conclusion = StreamingRunConclusion(
        exit_code=1,
        failure_reason="inactivity_stall",
        retries_attempted=0,
    )
    extraction = _extraction_with_report(None)

    exit_override, failure_override = _inactivity_terminal_outcome(extraction)
    if exit_override is not None:
        conclusion.exit_code = exit_override
    conclusion.failure_reason = failure_override

    assert conclusion.exit_code == 1
    assert conclusion.failure_reason == "stalled"
    assert conclusion.retries_attempted == 0
