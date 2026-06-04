from pathlib import Path

import pytest

from meridian.lib.core.types import ArtifactKey, SpawnId
from meridian.lib.launch.extract import (
    FinalizeReportKind,
    _persist_report,
    classify_finalize_report,
)
from meridian.lib.launch.report import ExtractedReport, extract_or_fallback_report
from meridian.lib.state.artifact_store import LocalStore

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
OPENCODE_MESSAGE_PART_DELTA = (
    '{"id":"evt_delta","properties":{"part":{"messageID":"msg_1",'
    '"sessionID":"ses_1","text":"O","type":"text"},"sessionID":"ses_1"},'
    '"type":"message.part.delta"}'
)


def test_persist_report_wraps_assistant_extract_with_report_heading(tmp_path: Path) -> None:
    artifacts = LocalStore(root_dir=tmp_path / "artifacts")
    spawn_id = SpawnId("p-extract")
    log_dir = tmp_path / "spawns" / str(spawn_id)
    log_dir.mkdir(parents=True, exist_ok=True)

    report_path = _persist_report(
        artifacts=artifacts,
        spawn_id=spawn_id,
        log_dir=log_dir,
        extracted=ExtractedReport(content="Done.", source="assistant_message"),
        secrets=(),
    )

    assert report_path == log_dir / "report.md"
    expected = "# Report\n\nDone.\n"
    assert report_path.read_text(encoding="utf-8") == expected
    assert artifacts.get(ArtifactKey(f"{spawn_id}/report.md")).decode("utf-8") == expected


def test_classify_finalize_report_rejects_codex_close_error_payload() -> None:
    report = ExtractedReport(
        content='{"type":"error/connectionClosed","message":"no close frame received or sent"}',
        source="assistant_message",
    )

    assert classify_finalize_report(report) is FinalizeReportKind.CONTROL_FRAME


def test_classify_finalize_report_keeps_genuine_json_completion() -> None:
    report = ExtractedReport(content='{"message":"Done."}', source="assistant_message")

    assert classify_finalize_report(report) is FinalizeReportKind.DURABLE_COMPLETION


@pytest.mark.parametrize(
    "content",
    [
        OPENCODE_LIVE_MESSAGE_PART_UPDATED,
        OPENCODE_MESSAGE_PART_DELTA,
        '{"type":"message.updated","properties":{"info":{"role":"assistant"}}}',
        '{"type":"server.connected","properties":{}}',
    ],
)
def test_classify_finalize_report_rejects_opencode_message_event_envelopes(
    content: str,
) -> None:
    report = ExtractedReport(content=content, source="assistant_message")

    assert classify_finalize_report(report) is FinalizeReportKind.CONTROL_FRAME


@pytest.mark.parametrize(
    "content",
    [
        (
            '{"threadId":"019e92fc-881c-7533-a6b3-ccaa89c0dd2e",'
            '"turn":{"completedAt":null,"durationMs":null,"error":null,'
            '"id":"019e92fc-88d3-7430-9b93-67a2230470c8","items":[],'
            '"itemsView":"notLoaded","startedAt":1780582484,"status":"inProgress"},'
            '"type":"turn/started"}'
        ),
        '{"type":"mcpServer/statusChanged","server":"docs","status":"connected"}',
    ],
)
def test_classify_finalize_report_rejects_codex_event_envelopes(content: str) -> None:
    report = ExtractedReport(content=content, source="assistant_message")

    assert classify_finalize_report(report) is FinalizeReportKind.CONTROL_FRAME


@pytest.mark.parametrize(
    "content",
    [
        '{"type":"result","is_error":true,"terminal_reason":"aborted_streaming","result":""}',
        '{"type":"user","message":{"role":"user","content":['
        '{"type":"text","text":"[Request interrupted by user]"}]}}',
        '{"type":"user","message":{"role":"user","content":['
        '{"type":"tool_result","is_error":true,"content":"Exit code 144"}]}}',
        '{"type":"assistant","message":{"role":"assistant","content":['
        '{"type":"tool_use","name":"Bash","input":{"command":"sleep 600"}}]}}',
        '{"type":"assistant","message":{"role":"assistant","content":['
        '{"type":"thinking","thinking":"I should call Bash."}]}}',
        '{"type":"system","subtype":"thinking_tokens","estimated_tokens":180}',
        '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed"}}',
        '{"type":"unknown_telemetry","request_id":"req_1"}',
        '{"type":"result","result":"OK"}',
    ],
)
def test_classify_finalize_report_rejects_claude_control_and_progress_envelopes(
    content: str,
) -> None:
    report = ExtractedReport(content=content, source="assistant_message")

    assert classify_finalize_report(report) is FinalizeReportKind.CONTROL_FRAME


def test_classify_finalize_report_keeps_claude_success_result() -> None:
    report = ExtractedReport(
        content=(
            '{"type":"result","is_error":false,'
            '"terminal_reason":"end_turn","result":"OK"}'
        ),
        source="assistant_message",
    )

    assert classify_finalize_report(report) is FinalizeReportKind.DURABLE_COMPLETION


def test_extract_or_fallback_report_ignores_codex_connection_closed_history(
    tmp_path: Path,
) -> None:
    artifacts = LocalStore(root_dir=tmp_path / "artifacts")
    spawn_id = SpawnId("p-codex-close")
    artifacts.put(
        ArtifactKey(f"{spawn_id}/history.jsonl"),
        b'{"event_type":"error/connectionClosed",'
        b'"payload":{"message":"no close frame received or sent"},'
        b'"seq":5}\n',
    )

    report = extract_or_fallback_report(artifacts, spawn_id)

    assert report.content is None
    assert report.source is None


def test_extract_or_fallback_report_ignores_claude_aborted_streaming_history(
    tmp_path: Path,
) -> None:
    artifacts = LocalStore(root_dir=tmp_path / "artifacts")
    spawn_id = SpawnId("p-claude-abort")
    artifacts.put(
        ArtifactKey(f"{spawn_id}/history.jsonl"),
        b'{"type":"result","is_error":true,'
        b'"terminal_reason":"aborted_streaming","result":""}\n',
    )

    report = extract_or_fallback_report(artifacts, spawn_id)

    assert report.content is None
    assert report.source is None


def test_extract_or_fallback_report_uses_failure_after_claude_user_interrupt(
    tmp_path: Path,
) -> None:
    artifacts = LocalStore(root_dir=tmp_path / "artifacts")
    spawn_id = SpawnId("p-claude-interrupt")
    artifacts.put(
        ArtifactKey(f"{spawn_id}/history.jsonl"),
        b'{"type":"user","message":{"role":"user","content":['
        b'{"type":"text","text":"[Request interrupted by user]"}]}}\n',
    )

    report = extract_or_fallback_report(
        artifacts,
        spawn_id,
        failure_reason="cancelled",
    )

    assert report.content == "cancelled"
    assert report.source == "failure_reason"


def test_extract_or_fallback_report_uses_failure_after_claude_tool_error(
    tmp_path: Path,
) -> None:
    artifacts = LocalStore(root_dir=tmp_path / "artifacts")
    spawn_id = SpawnId("p-claude-tool-error")
    artifacts.put(
        ArtifactKey(f"{spawn_id}/history.jsonl"),
        b'{"type":"user","message":{"role":"user","content":['
        b'{"type":"tool_result","is_error":true,"content":"Exit code 144"}]}}\n',
    )

    report = extract_or_fallback_report(
        artifacts,
        spawn_id,
        failure_reason="cancelled",
    )

    assert report.content == "cancelled"
    assert report.source == "failure_reason"


def test_extract_or_fallback_report_uses_failure_after_codex_command_progress(
    tmp_path: Path,
) -> None:
    artifacts = LocalStore(root_dir=tmp_path / "artifacts")
    spawn_id = SpawnId("p-codex-command-progress")
    artifacts.put(
        ArtifactKey(f"{spawn_id}/history.jsonl"),
        b'{"event_type":"item/started",'
        b'"payload":{"item":{"type":"commandExecution","command":"sleep 600"}},'
        b'"seq":5}\n',
    )

    report = extract_or_fallback_report(
        artifacts,
        spawn_id,
        failure_reason="terminated",
    )

    assert report.content == "terminated"
    assert report.source == "failure_reason"


def test_extract_or_fallback_report_uses_failure_after_codex_turn_started(
    tmp_path: Path,
) -> None:
    artifacts = LocalStore(root_dir=tmp_path / "artifacts")
    spawn_id = SpawnId("p-codex-turn-started")
    artifacts.put(
        ArtifactKey(f"{spawn_id}/history.jsonl"),
        (
            b'{"threadId":"019e92fc-881c-7533-a6b3-ccaa89c0dd2e",'
            b'"turn":{"completedAt":null,"durationMs":null,"error":null,'
            b'"id":"019e92fc-88d3-7430-9b93-67a2230470c8","items":[],'
            b'"itemsView":"notLoaded","startedAt":1780582484,"status":"inProgress"},'
            b'"type":"turn/started"}\n'
        ),
    )

    report = extract_or_fallback_report(
        artifacts,
        spawn_id,
        failure_reason="terminated",
    )

    assert report.content == "terminated"
    assert report.source == "failure_reason"


@pytest.mark.parametrize(
    "content",
    [
        OPENCODE_LIVE_MESSAGE_PART_UPDATED,
        OPENCODE_MESSAGE_PART_DELTA,
        '{"type":"server.connected","properties":{}}',
    ],
)
def test_extract_or_fallback_report_uses_failure_after_opencode_message_envelope(
    tmp_path: Path,
    content: str,
) -> None:
    artifacts = LocalStore(root_dir=tmp_path / "artifacts")
    spawn_id = SpawnId("p-opencode-message-envelope")
    artifacts.put(
        ArtifactKey(f"{spawn_id}/history.jsonl"),
        f"{content}\n".encode(),
    )

    report = extract_or_fallback_report(
        artifacts,
        spawn_id,
        failure_reason="cancelled",
    )

    assert report.content == "cancelled"
    assert report.source == "failure_reason"


def test_extract_or_fallback_report_uses_failure_after_related_codex_event(
    tmp_path: Path,
) -> None:
    artifacts = LocalStore(root_dir=tmp_path / "artifacts")
    spawn_id = SpawnId("p-codex-related-event")
    artifacts.put(
        ArtifactKey(f"{spawn_id}/history.jsonl"),
        b'{"event_type":"remoteControl/connected","payload":{"sessionId":"s1"},"seq":5}\n',
    )

    report = extract_or_fallback_report(
        artifacts,
        spawn_id,
        failure_reason="terminated",
    )

    assert report.content == "terminated"
    assert report.source == "failure_reason"
