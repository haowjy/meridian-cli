import pytest

from meridian.lib.launch.extract import FinalizeReportKind, classify_finalize_report
from meridian.lib.launch.report import ExtractedReport


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            '{"type":"meridian/error/connectionClosed","message":"connection lost"}',
            FinalizeReportKind.CONTROL_FRAME,
        ),
        (
            '{"type":"message.part.updated","properties":{"part":{"type":"tool"}}}',
            FinalizeReportKind.CONTROL_FRAME,
        ),
        (
            '{"type":"message.part.delta","properties":{"part":{"type":"text"}}}',
            FinalizeReportKind.CONTROL_FRAME,
        ),
        (
            '{"type":"server.connected","properties":{}}',
            FinalizeReportKind.CONTROL_FRAME,
        ),
        (
            '{"type":"turn/started","turn":{"status":"inProgress"}}',
            FinalizeReportKind.CONTROL_FRAME,
        ),
        (
            '{"type":"mcpServer/statusChanged","status":"connected"}',
            FinalizeReportKind.CONTROL_FRAME,
        ),
        (
            '{"type":"result","is_error":true,'
            '"terminal_reason":"aborted_streaming","result":""}',
            FinalizeReportKind.CONTROL_FRAME,
        ),
        (
            '{"type":"user","message":{"role":"user","content":'
            '[{"type":"text","text":"[Request interrupted by user]"}]}}',
            FinalizeReportKind.CONTROL_FRAME,
        ),
        (
            '{"type":"assistant","message":{"role":"assistant","content":'
            '[{"type":"tool_use","name":"Bash"}]}}',
            FinalizeReportKind.CONTROL_FRAME,
        ),
        (
            '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed"}}',
            FinalizeReportKind.CONTROL_FRAME,
        ),
        (
            '{"message":"Done."}',
            FinalizeReportKind.DURABLE_COMPLETION,
        ),
        (
            '{"type":"result","is_error":false,'
            '"terminal_reason":"end_turn","result":"OK"}',
            FinalizeReportKind.DURABLE_COMPLETION,
        ),
    ],
)
def test_classify_finalize_report_matrix(
    content: str,
    expected: FinalizeReportKind,
) -> None:
    report = ExtractedReport(content=content, source="assistant_message")

    assert classify_finalize_report(report) is expected
