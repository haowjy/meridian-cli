"""Unit tests for readable session-log rendering."""

from __future__ import annotations

from meridian.lib.core.util import FormatContext
from meridian.lib.harness.transcript import ToolCall
from meridian.lib.ops.session_log import SessionLogEntry, SessionLogEntryMessage, SessionLogOutput
from meridian.lib.ops.session_log_render import clean_content


def _entry(
    *,
    index: int,
    role: str,
    messages: tuple[SessionLogEntryMessage, ...],
) -> SessionLogEntry:
    content = "\n\n".join(message.content for message in messages)
    return SessionLogEntry(
        index=index,
        segment=0,
        segment_start_message=1,
        segment_end_message=max((message.segment_message for message in messages), default=0),
        role=role,
        content=content,
        messages=messages,
    )


def _output(*, entries: tuple[SessionLogEntry, ...], truncate: bool = True) -> SessionLogOutput:
    return SessionLogOutput(
        session_id="c100",
        requested_ref="c100",
        source="codex transcript",
        total_entries=len(entries),
        total_segments=1,
        segment_index=0,
        segment_entries=len(entries),
        segment_label="segment 0 (current)",
        showing="1-1" if len(entries) == 1 else "1-2",
        entries=entries,
        truncate=truncate,
    )


def _bash_call(msg_idx: int, command: str) -> SessionLogEntryMessage:
    return SessionLogEntryMessage(
        segment_message=msg_idx,
        role="assistant",
        content=f"[tool: bash {command}]",
        tool_call=ToolCall(name="bash", body=command),
    )


def _tool_result(msg_idx: int, content: str) -> SessionLogEntryMessage:
    return SessionLogEntryMessage(
        segment_message=msg_idx,
        role="user",
        content=f"[tool_result] {content}",
        is_tool_result=True,
    )


def test_clean_content_strips_known_wrappers() -> None:
    cleaned = clean_content(
        "<local-command-caveat>meta</local-command-caveat>"
        "<bash-input>echo hi</bash-input>\n"
        "<system_notification><status>running</status><summary>syncing</summary>"
        "</system_notification>"
    )
    assert "local-command-caveat" not in cleaned
    assert "$ echo hi" in cleaned
    assert "[notification: running — syncing]" in cleaned


def test_clean_mode_collapses_tool_calls() -> None:
    entry = _entry(
        index=1,
        role="mixed",
        messages=(
            _bash_call(1, "echo hi"),
            _tool_result(2, "<bash-stdout>hi</bash-stdout>"),
        ),
    )
    rendered = _output(entries=(entry,)).format_text()
    assert "  $ echo hi" in rendered
    assert "[tool_result]" not in rendered
    assert "Use --no-truncate to expand tool outputs" in rendered


def test_no_truncate_expands_tool_output() -> None:
    entry = _entry(
        index=1,
        role="mixed",
        messages=(
            _bash_call(1, "echo hi"),
            _tool_result(2, "<bash-stdout>hi</bash-stdout>"),
        ),
    )
    rendered = _output(entries=(entry,), truncate=False).format_text()
    assert "  $ echo hi" in rendered
    assert "  hi" in rendered


def test_raw_mode_preserves_verbose_layout() -> None:
    entry = _entry(
        index=4,
        role="mixed",
        messages=(
            _bash_call(10, "echo hi"),
            _tool_result(11, "<bash-stdout>hi</bash-stdout>"),
        ),
    )
    rendered = _output(entries=(entry,)).format_text(FormatContext(verbosity=1))
    assert "--- 4 [segment 0" in rendered
    assert "[tool_result]" in rendered


def test_codex_exec_command_collapses_to_dollar_sign() -> None:
    entry = _entry(
        index=1,
        role="mixed",
        messages=(
            SessionLogEntryMessage(
                segment_message=1,
                role="assistant",
                content='[tool: exec_command {"cmd":"ruff check ."}]',
                tool_call=ToolCall(name="bash", body="ruff check ."),
            ),
            _tool_result(2, "Process exited with code 0\nAll checks passed!"),
        ),
    )
    rendered = _output(entries=(entry,)).format_text()
    assert "  $ ruff check ." in rendered
    assert "exec_command" not in rendered


def test_codex_exec_command_failed() -> None:
    entry = _entry(
        index=1,
        role="mixed",
        messages=(
            SessionLogEntryMessage(
                segment_message=1,
                role="assistant",
                content='[tool: exec_command {"cmd":"ruff check ."}]',
                tool_call=ToolCall(name="bash", body="ruff check ."),
            ),
            _tool_result(2, "Process exited with code 1\nsrc/bad.py:10 error"),
        ),
    )
    rendered = _output(entries=(entry,)).format_text()
    assert "(failed: exit 1)" in rendered


def test_orphan_tool_call() -> None:
    entry = _entry(
        index=1,
        role="assistant",
        messages=(
            SessionLogEntryMessage(
                segment_message=1,
                role="assistant",
                content="[tool: read /tmp/file.txt]",
                tool_call=ToolCall(name="read", body="/tmp/file.txt"),
            ),
        ),
    )
    rendered = _output(entries=(entry,)).format_text()
    assert "  Read /tmp/file.txt" in rendered


def test_orphan_tool_result() -> None:
    entry = _entry(
        index=1,
        role="assistant",
        messages=(
            SessionLogEntryMessage(
                segment_message=1,
                role="assistant",
                content="[tool_result] <bash-stdout>hi</bash-stdout>",
                is_tool_result=True,
            ),
        ),
    )
    rendered = _output(entries=(entry,)).format_text()
    assert "(tool output)" in rendered
