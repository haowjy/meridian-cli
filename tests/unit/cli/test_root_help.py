"""Root help rendering contract tests."""

from __future__ import annotations

import re

from meridian.cli.command_groups import (
    AGENT_DESCRIPTION_OVERRIDES,
    AGENT_ROOT_COMMANDS,
    GROUP_DESCRIPTIONS,
)
from meridian.cli.startup.catalog import COMMAND_CATALOG
from meridian.cli.startup.help import LAUNCH_EXAMPLES, render_root_help

_ROW_RE = re.compile(r"^  (?P<left>\S+)\s{2,}(?P<right>.+)$")


def _section(help_text: str, heading: str) -> str:
    marker = f"{heading}:\n"
    start = help_text.index(marker) + len(marker)
    next_heading = re.search(r"\n\n[A-Z][A-Za-z /]+:\n", help_text[start:])
    if next_heading is None:
        return help_text[start:]
    return help_text[start : start + next_heading.start()]


def _commands(help_text: str) -> dict[str, str]:
    commands: dict[str, list[str]] = {}
    current: str | None = None
    for line in _section(help_text, "Commands").splitlines():
        match = _ROW_RE.match(line)
        if match:
            current = match.group("left")
            commands[current] = [match.group("right")]
        elif current is not None and line.strip():
            commands[current].append(line.strip())
    return {name: "\n".join(parts) for name, parts in commands.items()}


def test_agent_and_human_command_descriptions_use_registry() -> None:
    agent_commands = _commands(render_root_help(agent_mode=True))
    human_commands = _commands(render_root_help(agent_mode=False))

    for name in AGENT_ROOT_COMMANDS:
        assert agent_commands[name] == AGENT_DESCRIPTION_OVERRIDES.get(
            name, GROUP_DESCRIPTIONS[name]
        )
        assert human_commands[name] == GROUP_DESCRIPTIONS[name]


def test_agent_options_are_directory_only() -> None:
    options = _section(render_root_help(agent_mode=True), "Options")

    assert "-C, --directory PATH" in options
    assert "-m, --model TEXT" not in options
    assert "--harness TEXT" not in options


def test_agent_commands_are_curated_and_human_commands_are_complete() -> None:
    agent_commands = _commands(render_root_help(agent_mode=True))
    human_commands = _commands(render_root_help(agent_mode=False))

    assert tuple(agent_commands) == AGENT_ROOT_COMMANDS
    assert "config" not in agent_commands
    assert "doctor" not in agent_commands
    assert set(human_commands) == COMMAND_CATALOG.top_level_names()


def test_primary_launch_block_is_human_only() -> None:
    agent_help = render_root_help(agent_mode=True)
    human_help = render_root_help(agent_mode=False)

    assert "Primary launch/resume:" not in agent_help
    assert "Primary launch/resume:" in human_help
    launch = _section(human_help, "Primary launch/resume")
    for invocation, _description in LAUNCH_EXAMPLES:
        assert f"  {invocation}" in launch


def test_both_root_help_views_render_core_sections() -> None:
    for agent_mode in (True, False):
        help_text = render_root_help(agent_mode=agent_mode)
        assert "Usage:" in help_text
        assert "Options:" in help_text
        assert "Commands:" in help_text
