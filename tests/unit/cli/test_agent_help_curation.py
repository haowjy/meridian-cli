"""Tests for agent-mode CLI help curation."""

from __future__ import annotations

import io
import re
from collections.abc import Iterator
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

import pytest
from rich.console import Console

import meridian.cli.main as cli_main
from meridian.cli.agent_help import (
    AGENT_CORE_SUBCOMMANDS,
    AGENT_HELP_SUPPLEMENTS,
    AGENT_VISIBLE_SUBCOMMANDS,
    print_curated_group_help,
)
from meridian.cli.app_tree import app, report_app, spawn_app
from meridian.cli.help_tiers import ADVANCED_PARAMS
from tests.unit.cli.test_help_content import SPAWN_PROMPT_FILE_IDIOM


@pytest.fixture(scope="module", autouse=True)
def _register_groups_once() -> Iterator[None]:
    cli_main._register_commands_for_invocation(["--help"])
    yield


def _render_curated_help(
    group_name: str,
    *,
    agent_mode: bool,
    advanced: bool = False,
) -> str:
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=120)
    print_curated_group_help(
        app,
        [group_name, "--help"],
        group_name,
        agent_mode=agent_mode,
        advanced=advanced,
        console=console,
    )
    return buffer.getvalue()


_COMMAND_LINE = re.compile(r"^\s+([\w-]+):")


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


def _spawn_visibility_snapshot() -> dict[str, tuple[bool, Any]]:
    commands = spawn_app._commands
    return {
        name: (subcommand.show, subcommand.sort_key)
        for name, subcommand in commands.items()
        if not name.startswith("-")
    }


def _capture_help_via_main(
    group_name: str,
    *,
    agent_mode: bool,
    advanced: bool = False,
) -> str:
    mode_flag = "--agent" if agent_mode else "--human"
    advanced_args = ["--advanced"] if advanced else []
    buffer = io.StringIO()
    with redirect_stdout(buffer), pytest.raises(SystemExit) as exc_info:
        cli_main.main([mode_flag, group_name, *advanced_args, "--help"])
    assert exc_info.value.code in {0, None}
    return buffer.getvalue()


def _capture_spawn_help_via_main(*, agent_mode: bool, advanced: bool = False) -> str:
    return _capture_help_via_main("spawn", agent_mode=agent_mode, advanced=advanced)


def test_agent_mode_spawn_help_curates_subcommands_and_supplement() -> None:
    help_text = _render_curated_help("spawn", agent_mode=True)
    command_names = _command_names_in_order(help_text)

    assert "status" not in command_names
    assert "stats" not in command_names
    assert "show" in command_names
    assert "wait" in command_names
    assert "list" in command_names
    assert "children" not in command_names
    assert "report" not in command_names
    assert "done" not in command_names
    assert "rearm" not in command_names
    assert "cancel-all" not in command_names
    assert "Which subcommand when" not in help_text
    assert "Usage examples:" not in help_text
    assert "Quick start:" not in help_text
    assert "Playbook:" in help_text
    assert SPAWN_PROMPT_FILE_IDIOM in help_text
    assert "Core loop:" not in help_text
    assert "Agent Notes:" not in help_text
    assert "Treat finalizing as active" not in help_text

    expected_prefix = list(AGENT_CORE_SUBCOMMANDS["spawn"])
    assert command_names[: len(expected_prefix)] == expected_prefix


def test_agent_mode_spawn_advanced_help_shows_full_agent_tier() -> None:
    help_text = _render_curated_help("spawn", agent_mode=True, advanced=True)
    command_names = _command_names_in_order(help_text)

    assert "Usage examples:" in help_text
    assert "Agent Notes:" in help_text
    assert "Treat finalizing as active" in help_text
    for name in AGENT_VISIBLE_SUBCOMMANDS["spawn"]:
        assert name in command_names

    expected_prefix = list(AGENT_VISIBLE_SUBCOMMANDS["spawn"])
    assert command_names[: len(expected_prefix)] == expected_prefix


def test_human_mode_spawn_help_shows_all_subcommands() -> None:
    help_text = _render_curated_help("spawn", agent_mode=False)
    command_names = _command_names_in_order(help_text)

    assert "status" in command_names
    assert "stats" in command_names


def test_agent_mode_spawn_report_help_has_no_examples() -> None:
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=120)
    report_app.help_print(console=console)
    help_text = buffer.getvalue()

    assert "Usage examples:" not in help_text


def test_agent_mode_session_help_has_additive_supplement_only() -> None:
    help_text = _render_curated_help("session", agent_mode=True)

    assert "Which subcommand when" not in help_text
    assert "Refs take three forms" in help_text
    assert "REF forms:" not in help_text
    command_names = _command_names_in_order(help_text)
    assert "log" in command_names
    assert "search" in command_names


def test_agent_mode_work_help_has_artifact_placement_note() -> None:
    help_text = _render_curated_help("work", agent_mode=True)

    assert "Quick reference" not in help_text
    assert "$MERIDIAN_ACTIVE_WORK_DIR" in help_text


def test_in_process_agent_then_human_restores_spawn_help() -> None:
    _capture_spawn_help_via_main(agent_mode=True)
    human_help = _capture_spawn_help_via_main(agent_mode=False)
    command_names = _command_names_in_order(human_help)

    assert "status" in command_names
    assert "stats" in command_names


def test_agent_spawn_help_restores_singleton_before_next_invocation() -> None:
    before = _spawn_visibility_snapshot()
    _capture_spawn_help_via_main(agent_mode=True)

    cli_main._register_commands_for_invocation(["spawn", "list"])

    assert _spawn_visibility_snapshot() == before
    assert spawn_app._commands["status"].show is True
    assert spawn_app._commands["stats"].show is True
    assert "Agent Notes:" not in (spawn_app.help or "")


def test_in_process_human_then_agent_curates_spawn_help() -> None:
    _capture_spawn_help_via_main(agent_mode=False)
    agent_help = _capture_spawn_help_via_main(agent_mode=True)
    command_names = _command_names_in_order(agent_help)

    assert "status" not in command_names
    assert "stats" not in command_names
    assert "Quick start:" not in agent_help
    assert "Playbook:" in agent_help
    assert SPAWN_PROMPT_FILE_IDIOM in agent_help
    assert "Core loop:" not in agent_help
    assert "Agent Notes:" not in agent_help
    expected_prefix = list(AGENT_CORE_SUBCOMMANDS["spawn"])
    assert command_names[: len(expected_prefix)] == expected_prefix


def test_in_process_agent_advanced_spawn_help_shows_full_agent_set() -> None:
    agent_help = _capture_spawn_help_via_main(agent_mode=True, advanced=True)
    command_names = _command_names_in_order(agent_help)

    assert "Quick start:" not in agent_help
    assert "Usage examples:" in agent_help
    assert "Agent Notes:" in agent_help
    expected_prefix = list(AGENT_VISIBLE_SUBCOMMANDS["spawn"])
    assert command_names[: len(expected_prefix)] == expected_prefix


def test_spawn_help_advanced_params_panel_follows_help_tier() -> None:
    agent_advanced = _capture_spawn_help_via_main(agent_mode=True, advanced=True)
    assert ADVANCED_PARAMS.show is False

    human = _capture_spawn_help_via_main(agent_mode=False)
    assert ADVANCED_PARAMS.show is False

    agent_lean = _capture_spawn_help_via_main(agent_mode=True)
    assert ADVANCED_PARAMS.show is False

    advanced_commands = _command_names_in_order(agent_advanced)
    human_commands = _command_names_in_order(human)
    lean_commands = _command_names_in_order(agent_lean)

    assert "Advanced:" not in agent_lean
    assert "--approval" not in agent_lean
    assert "--verbose" not in agent_lean
    assert "FORK, --fork" not in agent_lean
    assert "FORK-FRESH, --fork-fresh" not in agent_lean
    assert "--continue" in agent_lean
    assert "Advanced:" in agent_advanced
    assert "--approval" in agent_advanced
    assert "--verbose" in agent_advanced
    assert "FORK, --fork" in agent_advanced
    assert "FORK-FRESH, --fork-fresh" in agent_advanced
    assert "Advanced:" in human
    assert "--approval" in human
    assert "status" in human_commands
    assert "stats" in human_commands
    assert "status" not in lean_commands
    assert "stats" not in lean_commands
    assert "children" in advanced_commands
    assert "children" not in lean_commands

    lean_direct = _render_curated_help("spawn", agent_mode=True)
    assert "Advanced:" not in lean_direct
    assert "status" not in _command_names_in_order(lean_direct)
    assert "children" not in _command_names_in_order(lean_direct)

    advanced_direct = _render_curated_help("spawn", agent_mode=True, advanced=True)
    assert "children" in _command_names_in_order(advanced_direct)


def test_non_tiered_group_rejects_advanced_help() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    with (
        redirect_stdout(stdout),
        redirect_stderr(stderr),
        pytest.raises(SystemExit) as exc_info,
    ):
        cli_main.main(["--agent", "doctor", "-h", "--advanced"])

    assert exc_info.value.code == 1
    assert stdout.getvalue() == ""
    assert 'Unknown option: "--advanced"' in stderr.getvalue()


def test_in_process_agent_then_human_restores_doctor_help() -> None:
    agent_help = _capture_help_via_main("doctor", agent_mode=True)
    human_help = _capture_help_via_main("doctor", agent_mode=False)

    assert "Agent Notes:" in agent_help
    assert "Agent Notes:" not in human_help


def test_in_process_human_then_agent_curates_doctor_help() -> None:
    human_help = _capture_help_via_main("doctor", agent_mode=False)
    agent_help = _capture_help_via_main("doctor", agent_mode=True)

    assert "Agent Notes:" not in human_help
    assert "Agent Notes:" in agent_help
    assert "Run when a spawn seems stuck" in agent_help


def test_agent_help_is_gated_to_help_requests() -> None:
    baseline = _spawn_visibility_snapshot()

    cli_main._register_commands_for_invocation(["spawn", "list"])

    assert _spawn_visibility_snapshot() == baseline
    assert spawn_app._commands["status"].show is True
    assert spawn_app._commands["stats"].show is True
    assert "Agent Notes:" not in (spawn_app.help or "")


def test_every_agent_help_supplement_resolves_to_registered_app() -> None:
    group_apps = cli_main._agent_help_group_apps()

    assert set(AGENT_HELP_SUPPLEMENTS) <= set(group_apps)
    assert set(AGENT_HELP_SUPPLEMENTS) <= cli_main._registered_command_groups
    for group_app in group_apps.values():
        assert hasattr(group_app, "help_epilogue")
