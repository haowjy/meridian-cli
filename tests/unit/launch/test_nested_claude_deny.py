from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from meridian.lib.catalog.agent_copy import agent_copy_enabled
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.request import LaunchCompositionSurface, LaunchRuntime, SpawnRequest
from meridian.lib.tools import PLATFORM_ORIGIN, ToolRule
from tests.support.fixtures import allow_headless_claude, write_agent
from tests.support.launch import (
    build_launch_context,
    stub_bundle_request_and_resolve,
    tool_rule,
)

if TYPE_CHECKING:
    from pytest import MonkeyPatch

_BUILTIN_AGENT_DENIES = {
    "Agent(Explore)",
    "Agent(Plan)",
    "Agent(General-purpose)",
    "Agent(general-purpose)",
}
_TASK_DENIES = {
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "TaskStop",
    "TaskUpdate",
    "TodoWrite",
}
_CLAUDE_PLATFORM_RULES = (
    ToolRule(
        capability="agent",
        action="deny",
        native=("Agent", "SendMessage", "ListAgents"),
        origin=PLATFORM_ORIGIN,
    ),
    ToolRule(
        capability="task",
        action="deny",
        native=tuple(sorted(_TASK_DENIES)),
        origin=PLATFORM_ORIGIN,
    ),
)


@pytest.fixture(autouse=True)
def _allow_headless(tmp_path: Path) -> None:
    allow_headless_claude(tmp_path)


def _runtime(tmp_path: Path, surface: LaunchCompositionSurface) -> LaunchRuntime:
    return LaunchRuntime(
        composition_surface=surface,
        runtime_root=(tmp_path / ".meridian").as_posix(),
        project_paths_project_root=tmp_path.as_posix(),
        project_paths_execution_cwd=tmp_path.as_posix(),
    )


def _launch_argv(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    *,
    surface: LaunchCompositionSurface,
    tool_rules: tuple[ToolRule, ...] = (),
) -> tuple[str, ...]:
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="haiku",
        harness=HarnessId.CLAUDE,
        platform_tool_rules=_CLAUDE_PLATFORM_RULES,
    )
    (tmp_path / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )
    context = build_launch_context(
        spawn_id="p-nested-deny",
        request=SpawnRequest(
            prompt="test",
            model="haiku",
            harness=HarnessId.CLAUDE.value,
            tool_rules=tool_rules,
        ),
        runtime=_runtime(tmp_path, surface),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )
    return context.binding.argv


def _tool_flag_entries(argv: tuple[str, ...], flag: str) -> tuple[str, ...]:
    entries: list[str] = []
    for index, token in enumerate(argv):
        if token == flag:
            entries.extend(argv[index + 1].split(","))
        elif token.startswith(f"{flag}="):
            entries.extend(token.partition("=")[2].split(","))
    return tuple(filter(None, entries))


def _settings_permission_entries(argv: tuple[str, ...], action: str) -> tuple[str, ...]:
    if "--settings" not in argv:
        return ()
    payload = json.loads(argv[argv.index("--settings") + 1])
    return tuple(payload.get("permissions", {}).get(action, ()))


@pytest.mark.parametrize(
    "tool_rules",
    [
        (),
        # An allowlist (`*: deny`, as the list form of `tools` writes) keeps the platform denies.
        (tool_rule("*", "deny"), tool_rule("read", "allow", "Read")),
    ],
)
def test_spawn_prepare_denies_native_delegation_and_builtin_agents(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    tool_rules: tuple[ToolRule, ...],
) -> None:
    argv = _launch_argv(
        tmp_path,
        monkeypatch,
        surface=LaunchCompositionSurface.SPAWN_PREPARE,
        tool_rules=tool_rules,
    )

    assert {"Agent", *_TASK_DENIES, *_BUILTIN_AGENT_DENIES}.issubset(
        _tool_flag_entries(argv, "--disallowedTools")
    )


def test_profile_opt_out_keeps_task_off_the_platform_deny_list(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    write_agent(tmp_path, name="tasker", model="haiku")
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="haiku",
        harness=HarnessId.CLAUDE,
        tool_rules=(tool_rule("task", "allow", "TaskCreate", "TaskGet"),),
        platform_tool_rules=_CLAUDE_PLATFORM_RULES,
    )
    (tmp_path / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n', encoding="utf-8")
    context = build_launch_context(
        spawn_id="p-task-opt-out",
        request=SpawnRequest(
            prompt="test", model="haiku", harness=HarnessId.CLAUDE.value, agent="tasker"
        ),
        runtime=_runtime(tmp_path, LaunchCompositionSurface.SPAWN_PREPARE),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    argv = context.binding.argv
    assert {"TaskCreate", "TaskGet"}.issubset(_settings_permission_entries(argv, "allow"))
    assert _TASK_DENIES.isdisjoint(_tool_flag_entries(argv, "--disallowedTools"))
    assert "Agent" in _tool_flag_entries(argv, "--disallowedTools")


def test_primary_surface_denies_agent_but_not_task(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    argv = _launch_argv(
        tmp_path,
        monkeypatch,
        surface=LaunchCompositionSurface.PRIMARY,
        tool_rules=(tool_rule("*", "allow"), tool_rule("bash", "deny", "Bash")),
    )

    disallowed = _tool_flag_entries(argv, "--disallowedTools")
    assert {"Agent", "Bash", *_BUILTIN_AGENT_DENIES}.issubset(disallowed)
    assert _TASK_DENIES.isdisjoint(disallowed)


def test_agent_copy_exception_keeps_generic_agent_only(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-sonnet-4-6",
        harness=HarnessId.CLAUDE,
        tool_rules=(
            tool_rule("bash", "allow", "Bash"),
            tool_rule("agent", "allow", "Agent"),
        ),
        platform_tool_rules=_CLAUDE_PLATFORM_RULES,
    )
    (tmp_path / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n\n'
        "[settings.meridian.agent_copy]\n"
        'harnesses = ["claude"]\n',
        encoding="utf-8",
    )
    context = build_launch_context(
        spawn_id="p-agent-copy",
        request=SpawnRequest(
            prompt="test",
            model="claude-sonnet-4-6",
            harness=HarnessId.CLAUDE.value,
        ),
        runtime=_runtime(tmp_path, LaunchCompositionSurface.SPAWN_PREPARE),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    allowed = _settings_permission_entries(context.binding.argv, "allow")
    disallowed = _tool_flag_entries(context.binding.argv, "--disallowedTools")
    assert "Agent" in allowed
    assert "Agent" not in disallowed
    assert {
        "Agent(Explore)",
        "Agent(Plan)",
        "Agent(General-purpose)",
        "Agent(general-purpose)",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskOutput",
        "TaskStop",
        "TaskUpdate",
        "TodoWrite",
    }.issubset(disallowed)


def test_agent_copy_setting_comes_from_ancestor_mars_root(tmp_path: Path) -> None:
    nested = tmp_path / "repo" / "subdirectory"
    nested.mkdir(parents=True)
    (tmp_path / "repo" / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n\n'
        "[settings.meridian.agent_copy]\n"
        'harnesses = ["claude"]\n',
        encoding="utf-8",
    )

    assert agent_copy_enabled(nested, HarnessId.CLAUDE)


def test_allow_builtin_agents_claude_cross_module_contract(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    (tmp_path / "meridian.toml").write_text(
        "[harness.claude]\nallow_builtin_agents = true\n", encoding="utf-8"
    )

    argv = _launch_argv(
        tmp_path,
        monkeypatch,
        surface=LaunchCompositionSurface.SPAWN_PREPARE,
    )

    disallowed = set(_tool_flag_entries(argv, "--disallowedTools"))
    assert _BUILTIN_AGENT_DENIES.isdisjoint(disallowed)
    assert "Agent" not in disallowed
    assert _TASK_DENIES.issubset(disallowed)
