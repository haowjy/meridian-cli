"""Tests for aligned plain help formatting."""

from __future__ import annotations

import io

from rich.console import Console

from meridian.cli.agent_help import AlignedPlainFormatter


def test_aligned_plain_formatter_indents_wrapped_description_lines() -> None:
    formatter = AlignedPlainFormatter()
    console = Console(file=io.StringIO(), force_terminal=False, width=80)
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=80)

    from cyclopts.help import HelpEntry, HelpPanel

    panel = HelpPanel(
        title="Parameters",
        format="parameter",
        entries=[
            HelpEntry(
                positive_names=("--from",),
                description=(
                    "Inherit context from a prior spawn or chat/session. Repeatable. "
                    "Also inherits the source's work item when neither --work nor the "
                    "ambient session provides one."
                ),
            )
        ],
    )
    formatter(console, console.options, panel)
    rendered = buffer.getvalue()

    lines = [line for line in rendered.splitlines() if line.strip()]
    assert lines[0] == "Parameters:"
    assert lines[1].startswith("  --from:")
    continuation = lines[2]
    assert continuation.startswith(" " * len("  --from: "))
    assert "source's work item" in continuation
    assert "  " not in continuation.lstrip()


def test_aligned_plain_formatter_indents_wide_flag_fallback_description() -> None:
    formatter = AlignedPlainFormatter()
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=80)

    from cyclopts.help import HelpEntry, HelpPanel

    panel = HelpPanel(
        title="Parameters",
        format="parameter",
        entries=[
            HelpEntry(
                positive_names=(
                    "BACKGROUND",
                    "--background",
                    "--bg",
                    "--no-background",
                    "--no-bg",
                ),
                description="Run detached; returns a spawn id without waiting.",
            )
        ],
    )
    formatter(console, console.options, panel)
    rendered = buffer.getvalue()

    rendered_lines = [line for line in rendered.splitlines() if line.strip()]
    assert rendered_lines[1] == "  BACKGROUND, --background, --bg, --no-background, --no-bg:"

    block_indent = "    "
    description_lines = [
        line for line in rendered_lines[2:] if line.startswith(block_indent)
    ]
    assert description_lines
    assert all(line.startswith(block_indent) for line in description_lines)
    assert len(block_indent) < len("  BACKGROUND, --background, --bg, --no-background, --no-bg:")
    assert all(len(line.split()) > 1 for line in description_lines)


def test_aligned_plain_formatter_wraps_very_wide_flag_header_at_fixed_indent() -> None:
    formatter = AlignedPlainFormatter()
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=80)

    from cyclopts.help import HelpEntry, HelpPanel

    description = (
        "Reset the task ping timer when the agent produces output or tool activity. "
        "Useful for long-running background tasks."
    )
    panel = HelpPanel(
        title="Parameters",
        format="parameter",
        entries=[
            HelpEntry(
                positive_names=(
                    "TASK-PING-RESET-ON-ACTIVITY",
                    "--task-ping-reset-on-activity",
                    "--no-task-ping-reset-on-activity",
                ),
                description=description,
            )
        ],
    )
    formatter(console, console.options, panel)
    rendered = buffer.getvalue()

    rendered_lines = [line for line in rendered.splitlines() if line.strip()]
    assert rendered_lines[1].startswith("  TASK-PING-RESET-ON-ACTIVITY,")

    block_indent = "    "
    description_lines = [
        line for line in rendered_lines if line.startswith(block_indent)
    ]
    assert description_lines
    assert all(line.startswith(block_indent) for line in description_lines)
    header_width = len("  TASK-PING-RESET-ON-ACTIVITY, --task-ping-reset-on-activity, ")
    assert len(block_indent) < header_width
    assert all(len(line.split()) > 1 for line in description_lines)
