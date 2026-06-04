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


def test_has_durable_report_completion_distinguishes_completion_from_cancel_artifacts() -> None:
    assert has_durable_report_completion("# Report\n\nDone.\n") is True
    assert has_durable_report_completion('{"message":"Done."}') is True
    assert (
        has_durable_report_completion(
            '{"message":"Root cause: missing WebSocket close frame handling."}'
        )
        is True
    )
    assert (
        has_durable_report_completion(
            '{"event_type":"cancelled","payload":{"status":"cancelled","error":"cancelled"}}'
        )
        is False
    )
    assert (
        has_durable_report_completion("# Spawn failed\n\nClaude subprocess exited with code 130.")
        is False
    )
    assert (
        has_durable_report_completion(
            '# Report\n\n{"type":"error","message":"no close frame received or sent"}\n'
        )
        is False
    )
    assert (
        has_durable_report_completion(
            '{"event_type":"item/started",'
            '"payload":{"item":{"type":"commandExecution","command":"sleep 600"}}}'
        )
        is False
    )
    assert (
        has_durable_report_completion(
            '{"type":"user","message":{"role":"user","content":['
            '{"type":"text","text":"[Request interrupted by user]"}]}}'
        )
        is False
    )
    assert (
        has_durable_report_completion(
            '{"type":"user","message":{"role":"user","content":['
            '{"type":"tool_result","is_error":true,"content":"Exit code 144"}]}}'
        )
        is False
    )
    assert (
        has_durable_report_completion(
            '{"type":"assistant","message":{"role":"assistant","content":['
            '{"type":"tool_use","name":"Bash","input":{"command":"sleep 600"}}]}}'
        )
        is False
    )
    assert (
        has_durable_report_completion(
            '{"type":"assistant","message":{"role":"assistant","content":['
            '{"type":"thinking","thinking":"I should call Bash."}]}}'
        )
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


def test_resolve_completion_cancel_precedence_uses_report_before_cancel() -> None:
    report_outcome = resolve_completion_cancel_precedence(
        durable_report_completion=True,
        cancel_requested=True,
        cancel_exit_code=143,
        cancel_error="terminated",
    )

    cancel_outcome = resolve_completion_cancel_precedence(
        durable_report_completion=False,
        cancel_requested=True,
        cancel_exit_code=143,
        cancel_error="terminated",
    )

    assert report_outcome is not None
    assert report_outcome.status == "succeeded"
    assert report_outcome.exit_code == 0
    assert report_outcome.error is None
    assert cancel_outcome is not None
    assert cancel_outcome.status == "cancelled"
    assert cancel_outcome.exit_code == 143
    assert cancel_outcome.error == "terminated"


def test_finalizing_membership_reflects_active_non_terminal_state() -> None:
    assert "finalizing" in ACTIVE_SPAWN_STATUSES
    assert "finalizing" not in TERMINAL_SPAWN_STATUSES
    assert is_active_spawn_status("finalizing") is True
