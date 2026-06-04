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

OPENCODE_LIVE_MESSAGE_PART_UPDATED = (
    '{"id":"evt_e930bc68d001L4oUTckzuyF1cX","properties":{"part":'
    '{"callID":"call_00_RgQT21ir86rHpjzaSHOA0775",'
    '"id":"prt_e930bc3890019mFXX9VDpKyVfj",'
    '"messageID":"msg_e930bbe400016R3GelVzaGNpp4",'
    '"sessionID":"ses_16cf44268ffeswweMeU0xmAtPb","state":{"input":'
    '{"command":"python3 -c \\"from pathlib import Path; '
    "Path('/tmp/meridian-pr310-live-1780583451-2427651/opencode/project/"
    "pr310_opencode_49a8582d35.started').write_text('started'); import time; "
    'time.sleep(600)\\"","description":"Run Python command that sleeps 600s",'
    '"timeout":620000},"metadata":{"description":"Run Python command that sleeps 600s",'
    '"output":""},"status":"running","time":{"start":1780583483021}},'
    '"tool":"bash","type":"tool"},"sessionID":"ses_16cf44268ffeswweMeU0xmAtPb",'
    '"time":1780583483021},"type":"message.part.updated"}'
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
            '# Report\n\n{"threadId":"019e92fc-881c-7533-a6b3-ccaa89c0dd2e",'
            '"turn":{"completedAt":null,"durationMs":null,"error":null,'
            '"id":"019e92fc-88d3-7430-9b93-67a2230470c8","items":[],'
            '"itemsView":"notLoaded","startedAt":1780582484,"status":"inProgress"},'
            '"type":"turn/started"}'
        )
        is False
    )
    assert has_durable_report_completion('{"type":"thread/updated","threadId":"t1"}') is False
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
    assert (
        has_durable_report_completion(f"# Report\n\n{OPENCODE_LIVE_MESSAGE_PART_UPDATED}\n")
        is False
    )
    assert (
        has_durable_report_completion(
            '{"type":"message.part.delta","properties":{"part":{"type":"text","text":"O"}}}'
        )
        is False
    )
    assert (
        has_durable_report_completion(
            '{"type":"message.updated","properties":{"info":{"role":"assistant"}}}'
        )
        is False
    )
    assert has_durable_report_completion('{"type":"server.connected","properties":{}}') is False


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
