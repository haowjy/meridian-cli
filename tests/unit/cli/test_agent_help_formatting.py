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
                positive_names=("--background", "--bg", "--no-background", "--no-bg"),
                description="Run detached; returns a spawn id without waiting.",
            )
        ],
    )
    formatter(console, console.options, panel)
    rendered = buffer.getvalue()

    rendered_lines = [line for line in rendered.splitlines() if line.strip()]
    flag_prefix = "  --background, --bg, --no-background, --no-bg: "
    continuation = next(
        line for line in rendered_lines if line.startswith(" " * len(flag_prefix)) and "without" in line
    )
    assert continuation.startswith(" " * len(flag_prefix))
