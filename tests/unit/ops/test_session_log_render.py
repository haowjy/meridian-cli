"""Unit tests for readable session-log rendering."""

from __future__ import annotations

from meridian.lib.core.util import FormatContext
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


def test_clean_content_strips_known_wrappers_and_keeps_unknown_tags() -> None:
    content = (
        "<local-command-caveat>meta</local-command-caveat>"
        "<bash-input>echo hi</bash-input>\n"
        "<local-command-stdout>\x1b[32mgreen\x1b[0m</local-command-stdout>\n"
        "<system_notification><status>running</status><summary>syncing</summary>"
        "</system_notification>\n"
        "<custom-tag>keep me</custom-tag>"
    )

    cleaned = clean_content(content)

    assert "local-command-caveat" not in cleaned
    assert "$ echo hi" in cleaned
    assert "green" in cleaned
    assert "\x1b[" not in cleaned
    assert "[notification: running — syncing]" in cleaned
    assert "<custom-tag>keep me</custom-tag>" in cleaned


def test_clean_content_collapses_command_triplet_in_any_order() -> None:
    ordered = clean_content(
        "<command-name>status</command-name>"
        "<command-message>status details</command-message>"
        "<command-args>--json --verbose</command-args>"
    )
    swapped = clean_content(
        "<command-message>status details</command-message>"
        "<command-name>status</command-name>"
        "<command-args>--json --verbose</command-args>"
    )
    already_prefixed = clean_content(
        "<command-name>/status</command-name>"
        "<command-message>status details</command-message>"
        "<command-args>--json</command-args>"
    )

    assert ordered == "/status --json --verbose"
    assert swapped == "/status --json --verbose"
    assert already_prefixed == "/status --json"


def test_clean_content_renders_adjacent_bash_stdout_and_stderr_with_separator() -> None:
    cleaned = clean_content("<bash-stdout>line one</bash-stdout><bash-stderr>boom</bash-stderr>")

    assert cleaned == "line one\nstderr: boom"


def test_clean_content_suppresses_no_output_marker_when_stderr_is_present() -> None:
    cleaned = clean_content("<bash-stdout></bash-stdout><bash-stderr>boom</bash-stderr>")

    assert cleaned == "stderr: boom"
    assert "(no output)" not in cleaned


def test_clean_content_separates_adjacent_bash_input_and_stderr_output() -> None:
    cleaned = clean_content(
        "<bash-input>run check</bash-input>"
        "<bash-stdout></bash-stdout>"
        "<bash-stderr>boom</bash-stderr>"
    )

    assert cleaned == "$ run check\nstderr: boom"


def test_session_log_clean_mode_collapses_tool_output_and_adds_hint() -> None:
    entry = _entry(
        index=1,
        role="mixed",
        messages=(
            SessionLogEntryMessage(
                segment_message=1,
                role="assistant",
                content="I'll run a command.",
            ),
            SessionLogEntryMessage(
                segment_message=2,
                role="assistant",
                content="[tool: bash echo hi]",
            ),
            SessionLogEntryMessage(
                segment_message=3,
                role="user",
                content="[tool_result] <bash-stdout>hi</bash-stdout>",
            ),
        ),
    )

    rendered = _output(entries=(entry,)).format_text()

    assert rendered.startswith("# Session c100")
    assert "**Mixed** [1]" in rendered
    assert "  $ echo hi" in rendered
    assert "[tool_result]" not in rendered
    assert "Use --no-truncate to expand tool outputs" in rendered


def test_session_log_clean_mode_no_truncate_expands_tool_output() -> None:
    entry = _entry(
        index=1,
        role="mixed",
        messages=(
            SessionLogEntryMessage(
                segment_message=1,
                role="assistant",
                content="[tool: bash echo hi]",
            ),
            SessionLogEntryMessage(
                segment_message=2,
                role="user",
                content="[tool_result] <bash-stdout>hi</bash-stdout>",
            ),
        ),
    )

    rendered = _output(entries=(entry,), truncate=False).format_text()

    assert "  $ echo hi" in rendered
    assert "  hi" in rendered
    assert "Use --no-truncate to expand tool outputs" not in rendered


def test_session_log_raw_mode_preserves_verbose_entry_layout() -> None:
    entry = _entry(
        index=4,
        role="mixed",
        messages=(
            SessionLogEntryMessage(
                segment_message=10,
                role="assistant",
                content="[tool: bash echo hi]",
            ),
            SessionLogEntryMessage(
                segment_message=11,
                role="user",
                content="[tool_result] <bash-stdout>hi</bash-stdout>",
            ),
        ),
    )

    rendered = _output(entries=(entry,)).format_text(FormatContext(verbosity=1))

    assert rendered.splitlines()[0] == "Session c100 (codex transcript) — showing 1-1 of 1 entry"
    assert "--- 4 [segment 0 · messages 1-11] [mixed] ---" in rendered
    assert "[message 10 · assistant]" in rendered
    assert "[tool_result] <bash-stdout>hi</bash-stdout>" in rendered


def test_session_log_clean_mode_truncates_non_tool_text_in_multi_message_entry() -> None:
    long_text = "A" * 9001
    entry = _entry(
        index=1,
        role="assistant",
        messages=(
            SessionLogEntryMessage(
                segment_message=1,
                role="assistant",
                content=long_text,
            ),
            SessionLogEntryMessage(
                segment_message=2,
                role="assistant",
                content="[tool: bash echo hi]",
            ),
            SessionLogEntryMessage(
                segment_message=3,
                role="user",
                content="[tool_result] <bash-stdout>hi</bash-stdout>",
            ),
        ),
    )

    rendered = _output(entries=(entry,)).format_text()

    assert "truncated:" in rendered
    assert "rerun with --no-truncate" in rendered
    assert "  $ echo hi" in rendered


def test_session_log_clean_mode_single_message_orphan_tool_call_is_readable() -> None:
    entry = _entry(
        index=1,
        role="assistant",
        messages=(
            SessionLogEntryMessage(
                segment_message=1,
                role="assistant",
                content="[tool: read /tmp/file.txt]",
            ),
        ),
    )

    rendered = _output(entries=(entry,)).format_text()

    assert "  Read /tmp/file.txt" in rendered
    assert "[tool:" not in rendered


def test_session_log_clean_mode_single_message_orphan_tool_result_is_readable() -> None:
    entry = _entry(
        index=1,
        role="assistant",
        messages=(
            SessionLogEntryMessage(
                segment_message=1,
                role="assistant",
                content="[tool_result] <bash-stdout>hi</bash-stdout>",
            ),
        ),
    )

    rendered = _output(entries=(entry,)).format_text()

    assert "  (tool output): hi" in rendered
    assert "[tool_result]" not in rendered
