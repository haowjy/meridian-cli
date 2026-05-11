from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.permissions import (
    CLAUDE_NATIVE_DELEGATION_TOOLS,
    compute_nested_claude_deny_additions,
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
    tools: tuple[str, ...] = (),
    disallowed_tools: tuple[str, ...] = (),
) -> None:
    profile_lines = [
        "---",
        f"name: {name}",
        f"harness: {HarnessId.CLAUDE.value}",
    ]
    if tools:
        profile_lines.append("tools:")
        profile_lines.extend(f"  - {tool}" for tool in tools)
    if disallowed_tools:
        profile_lines.append("disallowed-tools:")
        profile_lines.extend(f"  - {tool}" for tool in disallowed_tools)
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
    disallowed_tools: tuple[str, ...] = (),
) -> SpawnRequest:
    request = SpawnRequest(
        prompt="test",
        harness=harness.value,
        agent=agent,
        disallowed_tools=disallowed_tools,
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

    assert set(resolved_request.disallowed_tools) == CLAUDE_NATIVE_DELEGATION_TOOLS


def test_compute_nested_deny_excludes_opted_out_agent_tool() -> None:
    deny_additions = compute_nested_claude_deny_additions(
        profile_allowed_tools=("Agent",),
        existing_disallowed_tools=(),
    )

    assert set(deny_additions) == (CLAUDE_NATIVE_DELEGATION_TOOLS - {"Agent"})


def test_primary_surface_claude_does_not_add_implicit_deny(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    resolved_request = _build_context(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        composition_surface=LaunchCompositionSurface.PRIMARY,
        harness=HarnessId.CLAUDE,
        disallowed_tools=("Bash",),
    )

    assert resolved_request.disallowed_tools == ("Bash",)


def test_spawn_prepare_non_claude_does_not_add_implicit_deny(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    resolved_request = _build_context(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
        harness=HarnessId.CODEX,
        disallowed_tools=("Bash",),
    )

    assert resolved_request.disallowed_tools == ("Bash",)


def test_spawn_prepare_claude_skips_implicit_deny_when_allowlist_present(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _write_agent_profile(
        tmp_path=tmp_path,
        name="allowlist-agent",
        tools=("Agent",),
    )
    resolved_request = _build_context(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
        harness=HarnessId.CLAUDE,
        agent="allowlist-agent",
    )

    assert resolved_request.allowed_tools == ("Agent",)
    expected_implicit_deny = CLAUDE_NATIVE_DELEGATION_TOOLS - {"Agent"}
    assert set(resolved_request.disallowed_tools) == expected_implicit_deny


def test_spawn_prepare_claude_with_profile_tools_partial_optout(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _write_agent_profile(
        tmp_path=tmp_path,
        name="partial-optout-agent",
        tools=("Agent",),
    )
    resolved_request = _build_context(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
        harness=HarnessId.CLAUDE,
        agent="partial-optout-agent",
    )

    expected_implicit_deny = {
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskOutput",
        "TaskStop",
        "TaskUpdate",
    }
    assert resolved_request.allowed_tools == ("Agent",)
    assert expected_implicit_deny.issubset(set(resolved_request.disallowed_tools))
    assert "Agent" not in set(resolved_request.disallowed_tools)


def test_adhoc_allowed_tools_without_profile_still_denies_agent(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """S-9: Missing profile means no opt-outs."""
    request = SpawnRequest(
        prompt="test",
        harness=HarnessId.CLAUDE.value,
        allowed_tools=("Agent",),
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

    assert "Agent" not in context.resolved_request.allowed_tools
    assert "Agent" in context.resolved_request.disallowed_tools
    assert CLAUDE_NATIVE_DELEGATION_TOOLS.issubset(set(context.resolved_request.disallowed_tools))
    assert "--allowedTools" not in context.binding.argv


def test_adhoc_allowed_tools_respects_existing_explicit_deny_precedence(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    request = SpawnRequest(
        prompt="test",
        harness=HarnessId.CLAUDE.value,
        allowed_tools=("Agent", "Bash"),
        disallowed_tools=tuple(sorted(CLAUDE_NATIVE_DELEGATION_TOOLS)),
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

    assert context.resolved_request.allowed_tools == ("Bash",)
    assert "Agent" in context.resolved_request.disallowed_tools
    allowed_flag_index = context.binding.argv.index("--allowedTools")
    assert context.binding.argv[allowed_flag_index + 1] == "Bash"


def test_claude_keeps_only_nesting_sentinel_blocked_from_child_env() -> None:
    adapter = get_default_harness_registry().get_subprocess_harness(HarnessId.CLAUDE)

    assert adapter.blocked_child_env_vars() == {"CLAUDECODE"}
