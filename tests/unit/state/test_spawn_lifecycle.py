from meridian.lib.core.spawn_lifecycle import (
    ACTIVE_SPAWN_STATUSES,
    TERMINAL_SPAWN_STATUSES,
    ExecutionTerminalFacts,
    has_durable_report_completion,
    is_active_spawn_status,
    resolve_completion_cancel_precedence,
    resolve_execution_terminal_outcome,
    resolve_execution_terminal_state,
)


def test_has_durable_report_completion_rejects_cancelled_control_frame() -> None:
    assert (
        has_durable_report_completion(
            '{"event_type":"cancelled","payload":{"status":"cancelled","error":"cancelled"}}'
        )
        is False
    )


def test_has_durable_report_completion_rejects_generated_failure_markdown() -> None:
    assert (
        has_durable_report_completion("# Spawn failed\n\nClaude subprocess exited with code 130.")
        is False
    )


def test_resolve_execution_terminal_state_returns_cancelled_for_cancel_intent() -> None:
    status, exit_code, error = resolve_execution_terminal_state(
        exit_code=143,
        failure_reason="terminated",
        cancelled=True,
    )
    assert status == "cancelled"
    assert exit_code == 143
    assert error == "terminated"


def test_resolve_execution_terminal_state_prefers_durable_completion_over_cancel() -> None:
    status, exit_code, error = resolve_execution_terminal_state(
        exit_code=143,
        failure_reason="terminated",
        cancelled=True,
        durable_report_completion=True,
    )
    assert status == "succeeded"
    assert exit_code == 0
    assert error is None


def test_resolve_execution_terminal_outcome_projects_runner_facts() -> None:
    outcome = resolve_execution_terminal_outcome(
        ExecutionTerminalFacts(
            exit_code=143,
            failure_reason="terminated",
            cancellation_observed=True,
        )
    )

    assert outcome.status == "cancelled"
    assert outcome.exit_code == 143
    assert outcome.error == "terminated"


def test_resolve_completion_cancel_precedence_prefers_durable_report() -> None:
    outcome = resolve_completion_cancel_precedence(
        durable_report_completion=True,
        cancel_requested=True,
        cancel_exit_code=143,
        cancel_error="terminated",
    )

    assert outcome is not None
    assert outcome.status == "succeeded"
    assert outcome.exit_code == 0
    assert outcome.error is None


def test_resolve_completion_cancel_precedence_uses_cancel_intent_without_report() -> None:
    outcome = resolve_completion_cancel_precedence(
        durable_report_completion=False,
        cancel_requested=True,
        cancel_exit_code=143,
        cancel_error="terminated",
    )

    assert outcome is not None
    assert outcome.status == "cancelled"
    assert outcome.exit_code == 143
    assert outcome.error == "terminated"


def test_finalizing_membership_reflects_active_non_terminal_state() -> None:
    assert "finalizing" in ACTIVE_SPAWN_STATUSES
    assert "finalizing" not in TERMINAL_SPAWN_STATUSES
    assert is_active_spawn_status("finalizing") is True
