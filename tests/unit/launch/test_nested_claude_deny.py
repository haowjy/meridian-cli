# qa-validated: mars-launch-bundle-design
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.mars_bundle import MarsLaunchBundleUnavailableError
from meridian.lib.launch.permissions import (
    CLAUDE_NATIVE_DELEGATION_TOOLS,
    compute_nested_claude_deny_additions,
    tools_field_declares_claude_delegation_policy,
)
from meridian.lib.launch.request import LaunchCompositionSurface, LaunchRuntime, SpawnRequest

if TYPE_CHECKING:
    from pytest import MonkeyPatch


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
) -> SpawnRequest:
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: (
            _ for _ in ()
        ).throw(MarsLaunchBundleUnavailableError("test covers legacy profile tool policy")),
    )
    request = SpawnRequest(
        prompt="test",
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

    assert resolved_request.tools == {"*": "allow", "agent": "deny", "task": "deny"}


def test_compute_nested_deny_excludes_opted_out_agent_tool() -> None:
    deny_additions = compute_nested_claude_deny_additions(
        profile_tools={"agent": "allow"},
        existing_tools=None,
    )

    assert set(deny_additions) == {"task"}


def test_compute_nested_deny_excludes_case_variant_opted_out_agent_tool() -> None:
    deny_additions = compute_nested_claude_deny_additions(
        profile_tools={"Agent": "allow"},
        existing_tools=None,
    )

    assert set(deny_additions) == {"task"}


def test_tools_field_declares_claude_delegation_policy_for_abstract_keys() -> None:
    assert tools_field_declares_claude_delegation_policy(
        {"agent": "allow", "task": "deny"}
    )


def test_tools_field_declares_claude_delegation_policy_for_full_native_keys() -> None:
    native_tools = {tool: "allow" for tool in CLAUDE_NATIVE_DELEGATION_TOOLS}
    assert tools_field_declares_claude_delegation_policy(native_tools)


def test_tools_field_declares_claude_delegation_policy_rejects_partial_native_keys() -> None:
    assert not tools_field_declares_claude_delegation_policy(
        {"Agent": "allow", "TaskCreate": "allow"}
    )


def test_tools_field_ignores_non_delegation_keys() -> None:
    assert not tools_field_declares_claude_delegation_policy({"bash": "allow"})


def test_primary_surface_claude_does_not_add_implicit_deny(
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

    assert resolved_request.tools == {"*": "allow", "bash": "deny"}


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


def test_spawn_prepare_claude_skips_implicit_deny_when_allowlist_present(
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
    )

    assert resolved_request.tools == {"agent": "allow", "task": "deny"}


def test_adhoc_allowed_tools_without_profile_still_denies_agent(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """S-9: Missing profile means no opt-outs."""
    _ = monkeypatch
    request = SpawnRequest(
        prompt="test",
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

    assert context.resolved_request.tools == {"*": "deny", "agent": "deny"}
    assert "--allowedTools" not in context.binding.argv


def test_adhoc_allowed_tools_respects_existing_explicit_deny_precedence(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _ = monkeypatch
    request = SpawnRequest(
        prompt="test",
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
    }
    allowed_flag_index = context.binding.argv.index("--allowedTools")
    assert context.binding.argv[allowed_flag_index + 1] == "Bash"


def test_adhoc_allowed_tools_preserves_native_key_casing_in_claude_flags(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _ = monkeypatch
    request = SpawnRequest(
        prompt="test",
        harness=HarnessId.CLAUDE.value,
        tools={"*": "deny", "mcp__github__create_issue": "allow"},
    )

    context = build_launch_context(
        spawn_id="p-adhoc-native-key",
        request=request,
        runtime=_build_launch_runtime(
            tmp_path=tmp_path,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    allowed_flag_index = context.binding.argv.index("--allowedTools")
    assert context.binding.argv[allowed_flag_index + 1] == "mcp__github__create_issue"


def test_adhoc_case_varied_native_task_key_triggers_native_task_deny_expansion(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _ = monkeypatch
    request = SpawnRequest(
        prompt="test",
        harness=HarnessId.CLAUDE.value,
        tools={"*": "allow", "taskcreate": "allow"},
    )

    context = build_launch_context(
        spawn_id="p-adhoc-native-task-lower",
        request=request,
        runtime=_build_launch_runtime(
            tmp_path=tmp_path,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert context.resolved_request.tools == {
        "*": "allow",
        "taskcreate": "allow",
        "agent": "deny",
        "TaskGet": "deny",
        "TaskList": "deny",
        "TaskOutput": "deny",
        "TaskStop": "deny",
        "TaskUpdate": "deny",
    }
    assert "task" not in context.resolved_request.tools
    allowed_flag_index = context.binding.argv.index("--allowedTools")
    assert context.binding.argv[allowed_flag_index + 1] == "TaskCreate"
    disallowed_flag_index = context.binding.argv.index("--disallowedTools")
    assert "TaskCreate" not in context.binding.argv[disallowed_flag_index + 1].split(",")


def test_claude_keeps_only_nesting_sentinel_blocked_from_child_env() -> None:
    adapter = get_default_harness_registry().get_subprocess_harness(HarnessId.CLAUDE)

    assert adapter.blocked_child_env_vars() == {"CLAUDECODE"}
