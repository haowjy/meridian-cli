"""Behavior-level contract tests for CLI help rendering."""

from __future__ import annotations

import io
import re
import sys
from contextlib import redirect_stdout

import pytest

import meridian.cli.main as cli_main
from meridian.cli.command_groups import (
    AGENT_CORE_SUBCOMMANDS,
    AGENT_VISIBLE_SUBCOMMANDS,
    supports_advanced_help,
)

SPAWN_PROMPT_FILE_IDIOM = "meridian work path prompts/"

_GROUPS_WITH_LEAVES = (
    "spawn",
    "session",
    "work",
    "config",
    "doctor",
    "mars",
    "ext",
    "hooks",
    "sync",
    "telemetry",
)

_COMMAND_LINE = re.compile(r"^\s+([\w-]+):")


def _capture_help(args: list[str], *, agent_mode: bool = False, advanced: bool = False) -> str:
    mode_args = ["--mode", "agent" if agent_mode else "human"]
    advanced_args = ["--advanced"] if advanced else []
    buffer = io.StringIO()
    with redirect_stdout(buffer), pytest.raises(SystemExit) as exc_info:
        cli_main.main([*mode_args, *args, *advanced_args, "--help"])
    assert exc_info.value.code in {0, None}
    return buffer.getvalue()


def _command_names_in_order(help_text: str) -> list[str]:
    names: list[str] = []
    in_commands = False
    for line in help_text.splitlines():
        stripped = line.strip()
        if stripped == "Commands:":
            in_commands = True
            continue
        if not in_commands:
            continue
        if stripped.startswith(("Parameters:", "Usage examples:", "Agent Notes:", "Playbook:")):
            break
        match = _COMMAND_LINE.match(line)
        if match:
            names.append(match.group(1))
    return names


def _section_present(help_text: str, heading: str) -> bool:
    return f"{heading}:" in help_text


def test_no_group_or_leaf_help_contains_root_launch_resume() -> None:
    launch_heading = "Primary launch/resume"
    for group in _GROUPS_WITH_LEAVES:
        assert not _section_present(_capture_help([group], agent_mode=False), launch_heading)
        assert not _section_present(_capture_help([group], agent_mode=True), launch_heading)

    for args in (["spawn", "show"], ["spawn", "list"], ["config", "get"]):
        assert not _section_present(_capture_help(list(args), agent_mode=False), launch_heading)
        assert not _section_present(_capture_help(list(args), agent_mode=True), launch_heading)


def test_spawn_group_help_tiers_and_leaf_isolation() -> None:
    human_group = _capture_help(["spawn"], agent_mode=False)
    agent_group = _capture_help(["spawn"], agent_mode=True)
    agent_advanced = _capture_help(["spawn"], agent_mode=True, advanced=True)
    human_leaf = _capture_help(["spawn", "show"], agent_mode=False)

    assert _section_present(human_group, "Usage examples")
    assert not _section_present(agent_group, "Usage examples")
    assert _section_present(agent_group, "Playbook")
    assert SPAWN_PROMPT_FILE_IDIOM in agent_group
    assert not _section_present(agent_group, "Agent Notes")
    assert _section_present(agent_advanced, "Agent Notes")
    assert _section_present(agent_advanced, "Usage examples")

    assert not _section_present(human_leaf, "Usage examples")
    assert not _section_present(human_leaf, "Agent Notes")
    assert not _section_present(human_leaf, "Playbook")


def test_spawn_help_round_trips_in_process_without_sticky_state() -> None:
    human_first = _capture_help(["spawn"], agent_mode=False)
    agent_first = _capture_help(["spawn"], agent_mode=True)
    human_second = _capture_help(["spawn"], agent_mode=False)
    agent_second = _capture_help(["spawn"], agent_mode=True)

    assert human_first == human_second
    assert agent_first == agent_second


def test_agent_spawn_help_curates_subcommands() -> None:
    help_text = _capture_help(["spawn"], agent_mode=True)
    command_names = _command_names_in_order(help_text)

    assert "status" not in command_names
    assert "stats" not in command_names
    core = AGENT_CORE_SUBCOMMANDS["spawn"]
    assert command_names[: len(core)] == list(core)


def test_agent_spawn_advanced_help_shows_full_set() -> None:
    help_text = _capture_help(["spawn"], agent_mode=True, advanced=True)
    command_names = _command_names_in_order(help_text)

    assert _section_present(help_text, "Agent Notes")
    assert command_names[: len(AGENT_VISIBLE_SUBCOMMANDS["spawn"])] == list(
        AGENT_VISIBLE_SUBCOMMANDS["spawn"]
    )


def test_human_spawn_help_shows_hidden_subcommands() -> None:
    command_names = _command_names_in_order(_capture_help(["spawn"], agent_mode=False))
    assert "status" in command_names
    assert "stats" in command_names


def test_spawn_help_advanced_params_follow_tier() -> None:
    agent_lean = _capture_help(["spawn"], agent_mode=True)
    agent_advanced = _capture_help(["spawn"], agent_mode=True, advanced=True)

    assert "Advanced:" not in agent_lean
    assert "--approval" not in agent_lean
    assert "Advanced:" in agent_advanced
    assert "--approval" in agent_advanced
    assert "children" in _command_names_in_order(agent_advanced)
    assert "children" not in _command_names_in_order(agent_lean)


def test_non_tiered_group_rejects_advanced_help(monkeypatch: pytest.MonkeyPatch) -> None:
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    with redirect_stdout(io.StringIO()), pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--mode", "agent", "doctor", "-h", "--advanced"])

    assert exc_info.value.code == 1
    assert supports_advanced_help("doctor") is False
    assert 'Unknown option: "--advanced"' in stderr.getvalue()


def test_doctor_help_includes_agent_notes_only_in_agent_mode() -> None:
    human_help = _capture_help(["doctor"], agent_mode=False)
    agent_help = _capture_help(["doctor"], agent_mode=True)

    assert not _section_present(human_help, "Agent Notes")
    assert _section_present(agent_help, "Agent Notes")
