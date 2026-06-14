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
    commands: dict[str, str] = {}
    for line in _section(help_text, "Commands").splitlines():
        match = _ROW_RE.match(line)
        if match:
            commands[match.group("left")] = match.group("right")
    return commands


def test_agent_and_human_command_descriptions_use_shared_source() -> None:
    agent_commands = _commands(render_root_help(agent_mode=True))
    human_commands = _commands(render_root_help(agent_mode=False))

    for name in AGENT_ROOT_COMMANDS:
        assert agent_commands[name] == AGENT_DESCRIPTION_OVERRIDES.get(
            name, GROUP_DESCRIPTIONS[name]
        )
        assert human_commands[name] == GROUP_DESCRIPTIONS[name]


def test_agent_options_include_useful_subset_without_help_or_version() -> None:
    options = _section(render_root_help(agent_mode=True), "Options")

    assert "-C, --directory PATH" in options
    assert "-m, --model TEXT" in options
    assert "--harness TEXT" in options
    assert "--format TEXT" in options
    assert "--json" in options
    assert "--config TEXT" in options
    assert "-h, --help" not in options
    assert "-v, --version" not in options


def test_agent_commands_are_curated_and_human_commands_are_complete() -> None:
    agent_commands = _commands(render_root_help(agent_mode=True))
    human_commands = _commands(render_root_help(agent_mode=False))

    assert tuple(agent_commands) == AGENT_ROOT_COMMANDS
    assert "telemetry" not in agent_commands
    assert "migrate" not in agent_commands
    assert "qi" not in agent_commands

    assert "telemetry" in human_commands
    assert set(human_commands) == COMMAND_CATALOG.top_level_names()


def test_launch_examples_are_single_aligned_lines_without_blank_gaps() -> None:
    for agent_mode in (True, False):
        launch = _section(render_root_help(agent_mode=agent_mode), "Primary launch/resume")
        assert "\n\n  meridian " not in launch
        for invocation, description in LAUNCH_EXAMPLES:
            assert f"  {invocation}" in launch
            assert f"  {description}" in launch
            assert f"  {invocation}\n" not in launch


def test_both_root_help_views_render_core_sections() -> None:
    for agent_mode in (True, False):
        help_text = render_root_help(agent_mode=agent_mode)
        assert help_text.strip()
        assert "Usage:" in help_text
        assert "Options:" in help_text
        assert "Primary launch/resume:" in help_text
        assert "Commands:" in help_text
