from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.claude_preflight import CLAUDE_PARENT_ALLOWED_TOOLS_FLAG
from meridian.lib.harness.projections.project_claude import project_claude_spec_to_cli_args
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.permissions import compute_nested_claude_deny_additions
from meridian.lib.launch.request import LaunchCompositionSurface, LaunchRuntime, SpawnRequest
from meridian.lib.safety.permissions import PermissionConfig, ToolsPermissionResolver
from tests.support.launch import stub_bundle_request_and_resolve

if TYPE_CHECKING:
    from pytest import MonkeyPatch

_CLAUDE_BUILTIN_AGENT_DENIES = {
    "agent(Explore)": "deny",
    "agent(Plan)": "deny",
    "agent(General-purpose)": "deny",
    "agent(general-purpose)": "deny",
}


def _build_launch_runtime(
    *,
    tmp_path: Path,
    composition_surface: LaunchCompositionSurface,
) -> LaunchRuntime:
    return LaunchRuntime(
        composition_surface=composition_surface,
        report_output_path=(tmp_path / "report.md").as_posix(),
        runtime_root=(tmp_path / ".meridian").as_posix(),
        project_paths_project_root=tmp_path.as_posix(),
        project_paths_execution_cwd=tmp_path.as_posix(),
    )


def _write_agent_profile(
    *,
    tmp_path: Path,
    name: str,
    tools: str | dict[str, str] | None = None,
) -> None:
    profile_lines = [
        "---",
        f"name: {name}",
        f"harness: {HarnessId.CLAUDE.value}",
    ]
    if isinstance(tools, str):
        profile_lines.append(f"tools: {tools}")
    elif isinstance(tools, dict):
        profile_lines.append("tools:")
        for key, action in tools.items():
            profile_lines.append(f"  {key}: {action}")
    profile_lines.extend(("---", "", "# Agent", "", "Test profile body."))

    profile_path = tmp_path / ".mars" / "agents" / f"{name}.md"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text("\n".join(profile_lines), encoding="utf-8")


def _build_context(
    *,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    composition_surface: LaunchCompositionSurface,
    harness: HarnessId,
    agent: str | None = None,
    tools: str | dict[str, str] | None = None,
    bundle_tools_allowed: tuple[str, ...] = (),
    bundle_tools_disallowed: tuple[str, ...] = (),
) -> SpawnRequest:
    model = "haiku" if harness == HarnessId.CLAUDE else "gpt-5.4-mini"
    stub_bundle_request_and_resolve(
        monkeypatch,
        model=model,
        harness=harness,
        tools_allowed=bundle_tools_allowed,
        tools_disallowed=bundle_tools_disallowed,
    )
    mars_toml = tmp_path / "mars.toml"
    if not mars_toml.exists():
        mars_toml.write_text(
            '[settings]\ntargets = [".claude", ".codex", ".opencode"]\n',
            encoding="utf-8",
        )
    request = SpawnRequest(
        prompt="test",
        model=model,
        harness=harness.value,
        agent=agent,
        tools=tools,
    )

    context = build_launch_context(
        spawn_id="p-nested-boundary",
        request=request,
        runtime=_build_launch_runtime(
            tmp_path=tmp_path,
            composition_surface=composition_surface,
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )
    return context.resolved_request


def _tool_flag_entries(argv: tuple[str, ...], flag: str) -> tuple[str, ...]:
    values: list[str] = []
    for index, token in enumerate(argv):
        if token == flag and index + 1 < len(argv):
            values.extend(item for item in argv[index + 1].split(",") if item)
        elif token.startswith(f"{flag}="):
            values.extend(item for item in token.partition("=")[2].split(",") if item)
    return tuple(values)


def _build_spawn_prepare_preview(
    *,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
):
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-sonnet-4-6",
        harness=HarnessId.CLAUDE,
        tools_allowed=("Bash", "Bash(meridian spawn *)", "Agent"),
    )
    request = SpawnRequest(
        prompt="test",
        model="claude-sonnet-4-6",
        harness=HarnessId.CLAUDE.value,
        tools={
            "bash": "allow",
            "bash(meridian spawn *)": "allow",
            "agent": "allow",
        },
    )

    return build_launch_context(
        spawn_id="p-claude-native-agent-routing",
        request=request,
        runtime=_build_launch_runtime(
            tmp_path=tmp_path,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )


def test_spawn_prepare_claude_adds_full_implicit_deny_set(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    resolved_request = _build_context(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
        harness=HarnessId.CLAUDE,
    )

    assert resolved_request.tools == {
        "*": "allow",
        "agent": "deny",
        "task": "deny",
        **_CLAUDE_BUILTIN_AGENT_DENIES,
    }


def test_compute_nested_deny_excludes_opted_out_agent_tool() -> None:
    deny_additions = compute_nested_claude_deny_additions(
        profile_tools={"agent": "allow"},
        existing_tools=None,
    )

    assert set(deny_additions) == {"task"}


def test_primary_surface_claude_denies_agent_without_agent_copy(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    resolved_request = _build_context(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        composition_surface=LaunchCompositionSurface.PRIMARY,
        harness=HarnessId.CLAUDE,
        tools={"*": "allow", "bash": "deny"},
    )

    assert resolved_request.tools == {
        "*": "allow",
        "bash": "deny",
        "agent": "deny",
        **_CLAUDE_BUILTIN_AGENT_DENIES,
    }


def test_spawn_prepare_non_claude_does_not_add_implicit_deny(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    resolved_request = _build_context(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
        harness=HarnessId.CODEX,
        tools={"*": "allow", "bash": "deny"},
    )

    assert resolved_request.tools == {"*": "allow", "bash": "deny"}


def test_spawn_prepare_claude_allowlist_still_denies_agent_without_agent_copy(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _write_agent_profile(
        tmp_path=tmp_path,
        name="allowlist-agent",
        tools={"agent": "allow"},
    )
    resolved_request = _build_context(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
        harness=HarnessId.CLAUDE,
        agent="allowlist-agent",
        bundle_tools_allowed=("agent",),
    )

    assert resolved_request.tools == {
        "agent": "deny",
        "task": "deny",
        **_CLAUDE_BUILTIN_AGENT_DENIES,
    }


def test_adhoc_allowed_tools_without_profile_still_denies_agent(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """S-9: Missing profile means no opt-outs."""
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="haiku",
        harness=HarnessId.CLAUDE,
    )
    (tmp_path / "mars.toml").write_text(
        '[settings]\ntargets = [".claude", ".codex", ".opencode"]\n',
        encoding="utf-8",
    )
    request = SpawnRequest(
        prompt="test",
        model="haiku",
        harness=HarnessId.CLAUDE.value,
        tools={"*": "deny", "agent": "allow"},
    )

    context = build_launch_context(
        spawn_id="p-adhoc",
        request=request,
        runtime=_build_launch_runtime(
            tmp_path=tmp_path,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert context.resolved_request.tools == {
        "*": "deny",
        "agent": "deny",
        **_CLAUDE_BUILTIN_AGENT_DENIES,
    }


def test_adhoc_allowed_tools_respects_existing_explicit_deny_precedence(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="haiku",
        harness=HarnessId.CLAUDE,
    )
    (tmp_path / "mars.toml").write_text(
        '[settings]\ntargets = [".claude", ".codex", ".opencode"]\n',
        encoding="utf-8",
    )
    request = SpawnRequest(
        prompt="test",
        model="haiku",
        harness=HarnessId.CLAUDE.value,
        tools={"*": "deny", "agent": "allow", "bash": "allow", "task": "deny"},
    )

    context = build_launch_context(
        spawn_id="p-adhoc-existing-deny",
        request=request,
        runtime=_build_launch_runtime(
            tmp_path=tmp_path,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert context.resolved_request.tools == {
        "*": "deny",
        "agent": "deny",
        "bash": "allow",
        "task": "deny",
        **_CLAUDE_BUILTIN_AGENT_DENIES,
    }


def test_claude_keeps_only_nesting_sentinel_blocked_from_child_env() -> None:
    adapter = get_default_harness_registry().get_subprocess_harness(HarnessId.CLAUDE)

    assert adapter.blocked_child_env_vars() == {"CLAUDECODE"}


def test_spawn_prepare_claude_without_agent_copy_denies_generic_agent(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    (tmp_path / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )

    preview = _build_spawn_prepare_preview(tmp_path=tmp_path, monkeypatch=monkeypatch)

    allowed = _tool_flag_entries(preview.binding.argv, "--allowedTools")
    disallowed = _tool_flag_entries(preview.binding.argv, "--disallowedTools")
    assert allowed == ("Bash", "Bash(meridian spawn *)")
    assert "Agent" in disallowed
    assert "Agent(Explore)" in disallowed
    assert "Agent(Plan)" in disallowed
    assert "Agent(General-purpose)" in disallowed
    assert "Agent(general-purpose)" in disallowed


def test_spawn_prepare_claude_with_agent_copy_keeps_generic_agent(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    (tmp_path / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n\n'
        "[settings.meridian.agent_copy]\n"
        'harnesses = ["claude"]\n',
        encoding="utf-8",
    )

    preview = _build_spawn_prepare_preview(tmp_path=tmp_path, monkeypatch=monkeypatch)

    allowed = _tool_flag_entries(preview.binding.argv, "--allowedTools")
    disallowed = _tool_flag_entries(preview.binding.argv, "--disallowedTools")
    assert allowed == ("Bash", "Bash(meridian spawn *)", "Agent")
    assert "Agent" not in disallowed
    assert "Agent(Explore)" in disallowed
    assert "Agent(Plan)" in disallowed
    assert "Agent(General-purpose)" in disallowed
    assert "Agent(general-purpose)" in disallowed


def test_parent_allowed_tools_cannot_readd_denied_agent() -> None:
    resolver = ToolsPermissionResolver(
        tools={
            "bash": "allow",
            "agent": "deny",
            "agent(Explore)": "deny",
        },
        fallback_config=PermissionConfig(),
    )
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CLAUDE,
        model="claude-sonnet-4-6",
        permission_resolver=resolver,
        extra_args=(CLAUDE_PARENT_ALLOWED_TOOLS_FLAG, "Agent,Agent(Explore),Write"),
        claude_native_agents_enabled=False,
    )

    argv = tuple(project_claude_spec_to_cli_args(spec, base_command=("claude",)))

    allowed = _tool_flag_entries(argv, "--allowedTools")
    disallowed = _tool_flag_entries(argv, "--disallowedTools")
    assert "Agent" not in allowed
    assert "Agent(Explore)" not in allowed
    assert "Write" in allowed
    assert "Agent" in disallowed
    assert "Agent(Explore)" in disallowed
