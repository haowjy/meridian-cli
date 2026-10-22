from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.claude_preflight import CLAUDE_PARENT_ALLOWED_TOOLS_FLAG
from meridian.lib.harness.projections.permissions_claude import project_has_claude_agent_copy
from meridian.lib.harness.projections.project_claude import project_claude_spec_to_cli_args
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.permissions import native_subagent_capabilities
from meridian.lib.launch.request import LaunchCompositionSurface, LaunchRuntime, SpawnRequest
from meridian.lib.safety.policy import build_permission_policy
from meridian.lib.tools import ToolsField
from tests.support.fixtures import allow_headless_claude
from tests.support.launch import build_launch_context, stub_bundle_request_and_resolve

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
}


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
    tools: ToolsField | None = None,
) -> tuple[str, ...]:
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="haiku",
        harness=HarnessId.CLAUDE,
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
            tools=tools,
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


def test_spawn_prepare_denies_native_delegation_and_builtin_agents(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    argv = _launch_argv(tmp_path, monkeypatch, surface=LaunchCompositionSurface.SPAWN_PREPARE)

    assert {"Agent", *_TASK_DENIES, *_BUILTIN_AGENT_DENIES}.issubset(
        _tool_flag_entries(argv, "--disallowedTools")
    )


def test_profile_opt_out_keeps_agent_off_the_platform_deny_list() -> None:
    assert native_subagent_capabilities(
        tools={"agent": "allow"},
        profile_tools={"agent": "allow"},
        spawn=True,
        native_agent_copy=True,
    ) == ("task",)


def test_parent_allowed_tools_cannot_readd_denied_agent() -> None:
    policy = build_permission_policy(
        tools={"bash": "allow", "agent": "deny", "agent(Explore)": "deny"}, sandbox=None
    )
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CLAUDE,
        model="claude-sonnet-4-6",
        permission_policy=policy,
        extra_args=(CLAUDE_PARENT_ALLOWED_TOOLS_FLAG, "Agent,Agent(Explore),Write"),
    )

    argv = tuple(project_claude_spec_to_cli_args(spec, base_command=("claude",)))
    allowed = _tool_flag_entries(argv, "--allowedTools")
    disallowed = _tool_flag_entries(argv, "--disallowedTools")

    assert "Write" in allowed
    assert "Agent" not in allowed
    assert "Agent(Explore)" not in allowed
    assert {"Agent", "Agent(Explore)"}.issubset(disallowed)


def test_primary_surface_denies_agent_but_not_task(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    argv = _launch_argv(
        tmp_path,
        monkeypatch,
        surface=LaunchCompositionSurface.PRIMARY,
        tools={"*": "allow", "bash": "deny"},
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
        tools_allowed=("Bash", "Agent"),
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
            tools={"bash": "allow", "agent": "allow"},
        ),
        runtime=_runtime(tmp_path, LaunchCompositionSurface.SPAWN_PREPARE),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    allowed = _tool_flag_entries(context.binding.argv, "--allowedTools")
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

    assert project_has_claude_agent_copy(nested)
