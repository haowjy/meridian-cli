# qa-validated: pi-rpc-quiescence
import signal
from pathlib import Path

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import SpawnId
from meridian.lib.launch.env import scope_pi_session_dir_for_spawn
from meridian.lib.launch.extract import FinalizeExtraction
from meridian.lib.launch.report import ExtractedReport
from meridian.lib.launch.streaming_runner import (
    StreamingRunConclusion,
    _AttemptRuntime,
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


def _extraction_with_report(report_text: str | None) -> FinalizeExtraction:
    return FinalizeExtraction(
        usage=TokenUsage(total_cost_usd=1.25, input_tokens=10, output_tokens=20),
        harness_session_id=None,
        report_path=None,
        report=ExtractedReport(content=report_text, source="report_md"),
        output_is_empty=False,
    )


def test_absorb_attempt_updates_exit_code_and_terminal_flags() -> None:
    conclusion = StreamingRunConclusion()

    conclusion.absorb_attempt(_attempt(exit_code=7, terminal_observed=True))

    assert conclusion.exit_code == 7
    assert conclusion.final_attempt_terminal_observed is True
    assert conclusion.terminated_after_completion is False


def test_absorb_attempt_preserves_prior_watchdog_completion() -> None:
    conclusion = StreamingRunConclusion()

    conclusion.absorb_attempt(_attempt(exit_code=1, watchdog=True))
    conclusion.absorb_attempt(_attempt(exit_code=0, watchdog=False, terminal_observed=True))

    assert conclusion.exit_code == 0
    assert conclusion.terminated_after_completion is True
    assert conclusion.final_attempt_terminal_observed is True


def test_terminal_facts_treat_durable_report_watchdog_as_success() -> None:
    conclusion = StreamingRunConclusion(
        exit_code=1,
        failure_reason="report_watchdog",
        extracted=_extraction_with_report("done"),
        terminated_after_completion=True,
    )

    facts = conclusion.terminal_facts(received_signal=None)
    assert facts.exit_code == 1
    assert facts.failure_reason == "report_watchdog"
    assert facts.durable_report_completion is True
    assert facts.terminated_after_completion is True
    assert facts.cancellation_observed is False


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


def test_scope_pi_session_dir_for_spawn_updates_env_and_creates_directory(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    child_env = {"PI_CODING_AGENT_SESSION_DIR": str(session_root)}

    scope_pi_session_dir_for_spawn(
        child_env=child_env,
        spawn_id=SpawnId("p-pi-scope"),
    )

    scoped = Path(child_env["PI_CODING_AGENT_SESSION_DIR"])
    assert scoped == session_root / "p-pi-scope"
    assert scoped.is_dir()


def test_scope_pi_session_dir_for_spawn_is_noop_without_session_root() -> None:
    child_env: dict[str, str] = {}

    scope_pi_session_dir_for_spawn(
        child_env=child_env,
        spawn_id=SpawnId("p-pi-scope-noop"),
    )

    assert child_env == {}


def test_scope_pi_session_dir_for_spawn_is_idempotent_for_already_scoped_path(
    tmp_path: Path,
) -> None:
    scoped_root = tmp_path / "sessions" / "p-pi-scope-idempotent"
    child_env = {"PI_CODING_AGENT_SESSION_DIR": str(scoped_root)}

    scope_pi_session_dir_for_spawn(
        child_env=child_env,
        spawn_id=SpawnId("p-pi-scope-idempotent"),
    )

    assert Path(child_env["PI_CODING_AGENT_SESSION_DIR"]) == scoped_root
