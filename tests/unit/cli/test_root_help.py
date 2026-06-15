"""Root help rendering shared data tests."""

from __future__ import annotations

import re

from meridian.cli.startup.catalog import COMMAND_CATALOG
from meridian.cli.startup.help import (
    AGENT_DESCRIPTION_OVERRIDES,
    AGENT_ROOT_COMMANDS,
    GROUP_DESCRIPTIONS,
    LAUNCH_EXAMPLES,
    render_root_help,
)

_ROW_RE = re.compile(r"^  (?P<left>\S+)\s{2,}(?P<right>.+)$")


def _section(help_text: str, heading: str) -> str:
    marker = f"{heading}:\n"
    start = help_text.index(marker) + len(marker)
    next_heading = re.search(r"\n\n[A-Z][A-Za-z /]+:\n", help_text[start:])
    if next_heading is None:
        return help_text[start:]
    return help_text[start : start + next_heading.start()]


def _commands(help_text: str) -> dict[str, str]:
    """Parse the Commands section, joining multi-line (wrapped) descriptions."""
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


def test_agent_and_human_command_descriptions_use_shared_source() -> None:
    agent_commands = _commands(render_root_help(agent_mode=True))
    human_commands = _commands(render_root_help(agent_mode=False))

    for name in AGENT_ROOT_COMMANDS:
        assert agent_commands[name] == AGENT_DESCRIPTION_OVERRIDES.get(
            name, GROUP_DESCRIPTIONS[name]
        )
        assert human_commands[name] == GROUP_DESCRIPTIONS[name]


def test_agent_options_are_directory_only() -> None:
    """Agent-mode root surfaces only -C; human-launch/output options are dropped."""
    options = _section(render_root_help(agent_mode=True), "Options")

    assert "-C, --directory PATH" in options
    assert "-m, --model TEXT" not in options
    assert "--harness TEXT" not in options
    assert "--format TEXT" not in options
    assert "--json" not in options
    assert "--config TEXT" not in options
    assert "-h, --help" not in options
    assert "-v, --version" not in options


def test_agent_commands_are_curated_and_human_commands_are_complete() -> None:
    agent_commands = _commands(render_root_help(agent_mode=True))
    human_commands = _commands(render_root_help(agent_mode=False))

    assert tuple(agent_commands) == AGENT_ROOT_COMMANDS
    # Human-only surface (config/doctor) and noise commands stay out of agent help.
    assert "config" not in agent_commands
    assert "doctor" not in agent_commands
    assert "telemetry" not in agent_commands
    assert "migrate" not in agent_commands
    assert "qi" not in agent_commands

    assert "telemetry" in human_commands
    assert set(human_commands) == COMMAND_CATALOG.top_level_names()


def test_primary_launch_block_is_human_only() -> None:
    agent_help = render_root_help(agent_mode=True)
    human_help = render_root_help(agent_mode=False)

    assert "Primary launch/resume:" not in agent_help
    assert "Primary launch/resume:" in human_help

    launch = _section(human_help, "Primary launch/resume")
    assert "\n\n  meridian " not in launch
    for invocation, description in LAUNCH_EXAMPLES:
        assert f"  {invocation}" in launch
        assert f"  {description}" in launch
        assert f"  {invocation}\n" not in launch


def test_agent_help_routes_to_first_class_group_help() -> None:
    agent_help = render_root_help(agent_mode=True)
    assert "For details on any command:" in agent_help
    for group in ("spawn", "work", "session", "context"):
        assert f"`meridian {group} -h`" in agent_help


def test_both_root_help_views_render_core_sections() -> None:
    for agent_mode in (True, False):
        help_text = render_root_help(agent_mode=agent_mode)
        assert help_text.strip()
        assert "Usage:" in help_text
        assert "Options:" in help_text
        assert "Commands:" in help_text
