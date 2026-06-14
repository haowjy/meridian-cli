"""Regression tests for shared CLI help content."""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest

import meridian.cli.main as cli_main
from meridian.cli.agent_help import (
    AGENT_CORE_SUBCOMMANDS,
    AGENT_HELP_SUPPLEMENTS,
    AGENT_VISIBLE_SUBCOMMANDS,
)
from meridian.cli.help_content import GROUPS, render_group_help
from meridian.cli.startup.help import GROUP_DESCRIPTIONS

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


def _capture_help(args: list[str], *, agent_mode: bool = False) -> str:
    mode_flag = "--agent" if agent_mode else "--human"
    buffer = io.StringIO()
    with redirect_stdout(buffer), pytest.raises(SystemExit) as exc_info:
        cli_main.main([mode_flag, *args, "--help"])
    assert exc_info.value.code in {0, None}
    return buffer.getvalue()


def test_group_descriptions_are_view_over_shared_content() -> None:
    assert {name: group.summary for name, group in GROUPS.items()} == GROUP_DESCRIPTIONS
    assert {
        name: group.agent_notes for name, group in GROUPS.items() if group.agent_notes is not None
    } == AGENT_HELP_SUPPLEMENTS
    assert {
        name: group.agent_subcommands
        for name, group in GROUPS.items()
        if group.agent_subcommands is not None
    } == AGENT_VISIBLE_SUBCOMMANDS
    assert {
        name: group.agent_core_subcommands
        for name, group in GROUPS.items()
        if group.agent_core_subcommands is not None
    } == AGENT_CORE_SUBCOMMANDS


def test_no_group_or_leaf_help_contains_root_launch_resume() -> None:
    for group in _GROUPS_WITH_LEAVES:
        assert "Primary launch/resume" not in _capture_help([group], agent_mode=False)
        assert "Primary launch/resume" not in _capture_help([group], agent_mode=True)

    for args in (["spawn", "show"], ["spawn", "list"], ["config", "get"]):
        assert "Primary launch/resume" not in _capture_help(list(args), agent_mode=False)
        assert "Primary launch/resume" not in _capture_help(list(args), agent_mode=True)


def test_spawn_examples_stay_on_group_help_not_leaves() -> None:
    human_group = _capture_help(["spawn"], agent_mode=False)
    agent_group = _capture_help(["spawn"], agent_mode=True)
    human_leaf = _capture_help(["spawn", "show"], agent_mode=False)
    agent_leaf = _capture_help(["spawn", "show"], agent_mode=True)

    assert "Usage examples:" in human_group
    assert "meridian spawn wait" in human_group
    assert "Usage examples:" not in agent_group
    assert "Quick start:" in agent_group
    assert "wait" in agent_group

    # Group-only content (examples + agent notes) must not leak onto leaves.
    for leaf in (human_leaf, agent_leaf):
        assert "Usage examples:" not in leaf
        assert "Agent Notes:" not in leaf
        assert "meridian spawn wait" not in leaf
        assert "Treat finalizing as active" not in leaf
    # Leaf still shows its own command description.
    assert "Show spawn status" in human_leaf


def test_spawn_help_round_trips_in_process() -> None:
    """Public-surface reversibility: toggling modes leaves no sticky state."""

    human_first = _capture_help(["spawn"], agent_mode=False)
    agent_first = _capture_help(["spawn"], agent_mode=True)
    human_second = _capture_help(["spawn"], agent_mode=False)
    agent_second = _capture_help(["spawn"], agent_mode=True)

    assert human_first == human_second
    assert agent_first == agent_second
    assert "Quick start:" in agent_first
    assert "Agent Notes:" not in human_first


def test_group_help_renderer_adds_only_agent_curation() -> None:
    human_spawn = render_group_help("spawn", agent_mode=False)
    agent_spawn = render_group_help("spawn", agent_mode=True, advanced=True)
    human_hooks = render_group_help("hooks", agent_mode=False)
    agent_hooks = render_group_help("hooks", agent_mode=True)

    assert "Agent Notes:" not in human_spawn
    assert "Agent Notes:" in agent_spawn
    assert human_spawn in agent_spawn
    assert human_hooks == agent_hooks


def test_spawn_agent_group_help_has_lean_and_advanced_tiers() -> None:
    lean_spawn = render_group_help("spawn", agent_mode=True, advanced=False)
    advanced_spawn = render_group_help("spawn", agent_mode=True, advanced=True)
    human_spawn = render_group_help("spawn", agent_mode=False)

    assert "Quick start:" in lean_spawn
    assert "meridian spawn -h --advanced" in lean_spawn
    assert "Before you launch" in lean_spawn
    assert "verifiable exit state" in lean_spawn
    assert "Context: pass one folder plus at most one source-of-truth file" in lean_spawn
    assert "Reuse: --continue resumes the same session" in lean_spawn
    assert "Usage examples:" not in lean_spawn
    assert "Agent Notes:" not in lean_spawn

    assert "Quick start:" not in advanced_spawn
    assert "Usage examples:" in advanced_spawn
    assert "Agent Notes:" in advanced_spawn
    assert "Reuse decision" in advanced_spawn

    assert "Quick start:" not in human_spawn
    assert "Agent Notes:" not in human_spawn
    assert "Usage examples:" in human_spawn
