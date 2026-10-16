import pytest

from meridian.lib.core.spawn_lifecycle import (
    DurableReportEvidence,
    classify_durable_report_text,
    has_durable_report_completion,
    parse_terminal_status,
    resolve_completion_cancel_precedence,
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


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "timed_out"])
def test_parse_terminal_status_accepts_domain_terminal_values(status: str) -> None:
    assert parse_terminal_status(status) == status


@pytest.mark.parametrize("status", ["queued", "running", "finalizing", "unknown"])
def test_parse_terminal_status_rejects_nonterminal_values(status: str) -> None:
    with pytest.raises(ValueError, match="Invalid terminal spawn status"):
        parse_terminal_status(status)


@pytest.mark.parametrize(
    ("content", "expected_evidence"),
    [
        pytest.param(
            "# Report\n\nDone.\n", DurableReportEvidence.COMPLETION, id="report"
        ),
        pytest.param(
            '{"message":"Done."}', DurableReportEvidence.COMPLETION, id="message"
        ),
        pytest.param(
            "# Report\n\n"
            '{"event_type":"permission.asked","payload":{"properties":'
            '{"permission":"external_directory"}}}',
            DurableReportEvidence.ABSENT,
            id="wrapped-permission",
        ),
        pytest.param(
            '{"id":"evt_1","properties":{"permission":"external_directory"},'
            '"type":"permission.asked"}',
            DurableReportEvidence.ABSENT,
            id="permission",
        ),
        pytest.param(
            '{"message":"Root cause: missing WebSocket close frame handling."}',
            DurableReportEvidence.COMPLETION,
            id="report-about-control-frame",
        ),
        pytest.param(
            '{"event_type":"cancelled","payload":{"status":"cancelled","error":"cancelled"}}',
            DurableReportEvidence.CONTROL_FRAME,
            id="cancelled",
        ),
        pytest.param(
            "# Spawn failed\n\nClaude subprocess exited with code 130.",
            DurableReportEvidence.SYNTHETIC_FAILURE,
            id="spawn-failed",
        ),
        pytest.param(
            '# Report\n\n{"type":"error","message":"no close frame received or sent"}\n',
            DurableReportEvidence.CONTROL_FRAME,
            id="error",
        ),
        pytest.param(
            '{"event_type":"item/started",'
            '"payload":{"item":{"type":"commandExecution","command":"sleep 600"}}}',
            DurableReportEvidence.CONTROL_FRAME,
            id="item-started",
        ),
        pytest.param(
            '# Report\n\n{"threadId":"019e92fc-881c-7533-a6b3-ccaa89c0dd2e",'
            '"turn":{"completedAt":null,"durationMs":null,"error":null,'
            '"id":"019e92fc-88d3-7430-9b93-67a2230470c8","items":[],'
            '"itemsView":"notLoaded","startedAt":1780582484,"status":"inProgress"},'
            '"type":"turn/started"}',
            DurableReportEvidence.CONTROL_FRAME,
            id="turn-started",
        ),
        pytest.param(
            '{"type":"thread/updated","threadId":"t1"}',
            DurableReportEvidence.CONTROL_FRAME,
            id="thread-updated",
        ),
        pytest.param(
            '{"type":"user","message":{"role":"user","content":['
            '{"type":"text","text":"[Request interrupted by user]"}]}}',
            DurableReportEvidence.CONTROL_FRAME,
            id="interrupted",
        ),
        pytest.param(
            '{"type":"user","message":{"role":"user","content":['
            '{"type":"tool_result","is_error":true,"content":"Exit code 144"}]}}',
            DurableReportEvidence.CONTROL_FRAME,
            id="tool-result",
        ),
        pytest.param(
            '{"type":"assistant","message":{"role":"assistant","content":['
            '{"type":"tool_use","name":"Bash","input":{"command":"sleep 600"}}]}}',
            DurableReportEvidence.CONTROL_FRAME,
            id="tool-use",
        ),
        pytest.param(
            '{"type":"assistant","message":{"role":"assistant","content":['
            '{"type":"thinking","thinking":"I should call Bash."}]}}',
            DurableReportEvidence.CONTROL_FRAME,
            id="thinking",
        ),
        pytest.param(
            f"# Report\n\n{OPENCODE_LIVE_MESSAGE_PART_UPDATED}\n",
            DurableReportEvidence.CONTROL_FRAME,
            id="message-part-updated",
        ),
        pytest.param(
            '{"type":"message.part.delta","properties":{"part":{"type":"text","text":"O"}}}',
            DurableReportEvidence.CONTROL_FRAME,
            id="message-part-delta",
        ),
        pytest.param(
            '{"type":"message.updated","properties":{"info":{"role":"assistant"}}}',
            DurableReportEvidence.CONTROL_FRAME,
            id="message-updated",
        ),
        pytest.param(
            '{"type":"server.connected","properties":{}}',
            DurableReportEvidence.CONTROL_FRAME,
            id="server-connected",
        ),
        pytest.param(
            '{"type":"meridian/error/connectionClosed","message":"connection lost"}',
            DurableReportEvidence.CONTROL_FRAME,
            id="connection-closed",
        ),
        pytest.param(
            '{"type":"mcpServer/statusChanged","status":"connected"}',
            DurableReportEvidence.CONTROL_FRAME,
            id="mcp-status",
        ),
        pytest.param(
            '{"type":"result","is_error":true,"terminal_reason":"aborted_streaming","result":""}',
            DurableReportEvidence.CONTROL_FRAME,
            id="aborted-result",
        ),
        pytest.param(
            '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed"}}',
            DurableReportEvidence.CONTROL_FRAME,
            id="rate-limit",
        ),
        pytest.param(
            '{"type":"result","is_error":false,"terminal_reason":"end_turn","result":"OK"}',
            DurableReportEvidence.COMPLETION,
            id="completed-result",
        ),
    ],
)
def test_has_durable_report_completion(
    content: str,
    expected_evidence: DurableReportEvidence,
) -> None:
    assert classify_durable_report_text(content) is expected_evidence
    assert has_durable_report_completion(content) is (
        expected_evidence is DurableReportEvidence.COMPLETION
    )


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param(
            {"exit_code": 143, "failure_reason": "terminated", "cancelled": True},
            ("cancelled", 143, "terminated"),
            id="cancel-intent",
        ),
        pytest.param(
            {
                "exit_code": 143,
                "failure_reason": "terminated",
                "cancelled": True,
                "durable_report_completion": True,
            },
            ("cancelled", 143, "terminated"),
            id="cancellation-outranks-report",
        ),
        pytest.param(
            {
                "exit_code": 0,
                "failure_reason": None,
                "cancelled": True,
                "durable_report_completion": True,
            },
            ("succeeded", 0, None),
            id="clean-cancel-keeps-report",
        ),
        pytest.param(
            {
                "exit_code": 1,
                "failure_reason": "resident_deadline_expired",
                "durable_report_completion": True,
                "terminal_status": "timed_out",
            },
            ("timed_out", 1, "resident_deadline_expired"),
            id="timeout-outranks-report",
        ),
        pytest.param(
            {
                "exit_code": 1,
                "failure_reason": "ignored",
                "durable_report_completion": True,
                "terminal_status": "succeeded",
            },
            ("succeeded", 0, None),
            id="terminal-success",
        ),
        pytest.param(
            {
                "exit_code": 1,
                "failure_reason": "crash",
                "durable_report_completion": True,
                "terminal_status": None,
            },
            ("failed", 1, "crash"),
            id="nonzero-outranks-report",
        ),
        pytest.param(
            {
                "exit_code": 1,
                "failure_reason": "resident_deadline_expired",
                "durable_report_completion": False,
                "terminal_status": "timed_out",
            },
            ("timed_out", 1, "resident_deadline_expired"),
            id="timeout-without-report",
        ),
    ],
)
def test_resolve_execution_terminal_state(
    kwargs: dict[str, object],
    expected: tuple[str, int | None, str | None],
) -> None:
    assert resolve_execution_terminal_state(**kwargs) == expected  # type: ignore[arg-type]


def test_resolve_completion_cancel_precedence_trusts_report_without_execution_evidence() -> None:
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


def test_resolve_completion_cancel_precedence_abnormal_exit_outranks_report() -> None:
    # With runner-exit evidence, the same rule as resolve_execution_terminal_state
    # applies: a killed attempt is not a success just because prose was recovered.
    outcome = resolve_completion_cancel_precedence(
        durable_report_completion=True,
        cancel_requested=True,
        execution_exit_code=143,
        execution_terminal_status="cancelled",
    )

    assert outcome is not None
    assert outcome.status == "cancelled"
    assert outcome.exit_code == 143


def test_resolve_completion_cancel_precedence_clean_exit_keeps_report() -> None:
    outcome = resolve_completion_cancel_precedence(
        durable_report_completion=True,
        cancel_requested=True,
        execution_exit_code=0,
    )

    assert outcome is not None
    assert outcome.status == "succeeded"
    assert outcome.exit_code == 0
