# qa-validated: test-suite-redesign
# qa-validated: mars-launch-bundle-design
"""Spawn prepare surface tests: reference routing, channel manifests, and inventory placement."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.mars_bundle import (
    BundleExecutionPolicy,
    BundlePromptSurface,
    BundleRouting,
    BundleScaffoldSlots,
    BundleSkillsMetadata,
    BundleTools,
    LaunchBundle,
    MarsLaunchBundleUnavailableError,
)
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from tests.support.fixtures import write_agent, write_skill

pytestmark = pytest.mark.slow


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )


def _mock_bundle_for_context() -> LaunchBundle:
    return LaunchBundle(
        version=1,
        agent="dev-orchestrator",
        routing=BundleRouting(model="gpt-5.4", model_token="gpt-5.4", harness="codex"),
        execution_policy=BundleExecutionPolicy(
            effort="medium",
            sandbox="workspace-write",
            approval="auto",
        ),
        prompt_surface=BundlePromptSurface(system_instruction="## Bundle System\nFollow bundle."),
        scaffold_slots=BundleScaffoldSlots(),
        tools=BundleTools(allowed=("Bash",), disallowed=("Write",), mcp=("github=gh",)),
        skills_metadata=BundleSkillsMetadata(loaded=("verification",), missing=()),
        provenance={"model_source": "mars"},
        warnings=("bundle warning",),
    )


def _mock_bundle_without_sandbox_with_codex_tools() -> LaunchBundle:
    return LaunchBundle(
        version=1,
        agent="dev-orchestrator",
        routing=BundleRouting(model="gpt-5.4", model_token="gpt-5.4", harness="codex"),
        execution_policy=BundleExecutionPolicy(
            effort="medium",
            approval="auto",
            sandbox=None,
        ),
        prompt_surface=BundlePromptSurface(system_instruction="## Bundle System\nFollow bundle."),
        scaffold_slots=BundleScaffoldSlots(),
        tools=BundleTools(allowed=("Bash",), disallowed=(), mcp=()),
        skills_metadata=BundleSkillsMetadata(loaded=(), missing=()),
        provenance={"model_source": "mars"},
        warnings=(),
    )


def _mock_bundle_with_opencode_harness_model() -> LaunchBundle:
    return LaunchBundle(
        version=1,
        agent="dev-orchestrator",
        routing=BundleRouting(
            model="gpt-5.5",
            model_token="gpt55",
            harness="opencode",
            harness_model="openai/gpt-5.5",
            harness_model_source="candidate-path",
            harness_model_confidence="high",
        ),
        execution_policy=BundleExecutionPolicy(
            effort="medium",
            approval="auto",
            sandbox="workspace-write",
        ),
        prompt_surface=BundlePromptSurface(system_instruction="## Bundle System\nFollow bundle."),
        scaffold_slots=BundleScaffoldSlots(),
        tools=BundleTools(allowed=(), disallowed=(), mcp=()),
        skills_metadata=BundleSkillsMetadata(loaded=(), missing=()),
        provenance={"model_source": "mars"},
        warnings=(),
    )


def _mock_bundle_with_claude_native_tool_keys() -> LaunchBundle:
    return LaunchBundle(
        version=1,
        agent="dev-orchestrator",
        routing=BundleRouting(
            model="claude-sonnet-4-5",
            model_token="claude-sonnet-4-5",
            harness="claude",
        ),
        execution_policy=BundleExecutionPolicy(
            effort="medium",
            sandbox="workspace-write",
            approval="auto",
        ),
        prompt_surface=BundlePromptSurface(system_instruction="## Bundle System\nFollow bundle."),
        scaffold_slots=BundleScaffoldSlots(),
        tools=BundleTools(
            allowed=("Bash(grep -R)", "mcp__github__create_issue"),
            disallowed=("Write(File)",),
            mcp=(),
        ),
        skills_metadata=BundleSkillsMetadata(loaded=(), missing=()),
        provenance={"model_source": "mars"},
        warnings=(),
    )


def _mock_bundle_with_claude_native_delegation_allow_tools() -> LaunchBundle:
    return LaunchBundle(
        version=1,
        agent="dev-orchestrator",
        routing=BundleRouting(
            model="claude-sonnet-4-5",
            model_token="claude-sonnet-4-5",
            harness="claude",
        ),
        execution_policy=BundleExecutionPolicy(
            effort="medium",
            sandbox="workspace-write",
            approval="auto",
        ),
        prompt_surface=BundlePromptSurface(system_instruction="## Bundle System\nFollow bundle."),
        scaffold_slots=BundleScaffoldSlots(),
        tools=BundleTools(
            allowed=(
                "Agent",
                "TaskCreate",
                "TaskGet",
                "TaskList",
                "TaskOutput",
                "TaskStop",
                "TaskUpdate",
                "mcp__github__create_issue",
            ),
            disallowed=(),
            mcp=(),
        ),
        skills_metadata=BundleSkillsMetadata(loaded=(), missing=()),
        provenance={"model_source": "mars"},
        warnings=(),
    )


def _mock_bundle_with_claude_partial_native_delegation_tools() -> LaunchBundle:
    return LaunchBundle(
        version=1,
        agent="dev-orchestrator",
        routing=BundleRouting(
            model="claude-sonnet-4-5",
            model_token="claude-sonnet-4-5",
            harness="claude",
        ),
        execution_policy=BundleExecutionPolicy(
            effort="medium",
            sandbox="workspace-write",
            approval="auto",
        ),
        prompt_surface=BundlePromptSurface(system_instruction="## Bundle System\nFollow bundle."),
        scaffold_slots=BundleScaffoldSlots(),
        tools=BundleTools(
            allowed=("Agent", "TaskCreate", "mcp__github__create_issue"),
            disallowed=(),
            mcp=(),
        ),
        skills_metadata=BundleSkillsMetadata(loaded=(), missing=()),
        provenance={"model_source": "mars"},
        warnings=(),
    )


def _mock_bundle_with_claude_no_delegation_policy_tools() -> LaunchBundle:
    return LaunchBundle(
        version=1,
        agent="dev-orchestrator",
        routing=BundleRouting(
            model="claude-sonnet-4-5",
            model_token="claude-sonnet-4-5",
            harness="claude",
        ),
        execution_policy=BundleExecutionPolicy(
            effort="medium",
            sandbox="workspace-write",
            approval="auto",
        ),
        prompt_surface=BundlePromptSurface(system_instruction="## Bundle System\nFollow bundle."),
        scaffold_slots=BundleScaffoldSlots(),
        tools=BundleTools(allowed=(), disallowed=(), mcp=()),
        skills_metadata=BundleSkillsMetadata(loaded=(), missing=()),
        provenance={"model_source": "mars"},
        warnings=(),
    )


def _force_legacy_profile_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: (
            _ for _ in ()
        ).throw(MarsLaunchBundleUnavailableError("test uses legacy profile projection")),
    )


def test_spawn_prepare_opencode_keeps_all_references_inline(
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    write_agent(tmp_path, name="dev-orchestrator", model="claude-sonnet-4-5")
    write_agent(tmp_path, name="reviewer", model="gpt-5.4")
    file_ref = tmp_path / "README.md"
    file_ref.write_text("# hello\n", encoding="utf-8")
    dir_ref = tmp_path / "src"
    dir_ref.mkdir()
    (dir_ref / "main.py").write_text("print('ok')\n", encoding="utf-8")

    preview = build_launch_context(
        spawn_id="dry-run-opencode-spawn-prepare",
        request=SpawnRequest(
            prompt="task prompt",
            prompt_is_composed=False,
            model="kimi-k2.6",
            harness="opencode",
            reference_files=(file_ref.as_posix(), dir_ref.as_posix()),
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert "--file" not in preview.binding.argv
    assert file_ref.as_posix() not in preview.binding.argv
    assert preview.projected_content is not None
    assert [route.to_dict() for route in preview.projected_content.reference_routing] == [
        {
            "path": file_ref.as_posix(),
            "type": "file",
            "routing": "inline",
            "native_flag": None,
        },
        {
            "path": dir_ref.as_posix(),
            "type": "directory",
            "routing": "inline",
            "native_flag": None,
        },
    ]
    assert preview.projected_content.channel_manifest() == {
        "system_instruction": "system-field",
        "user_task_prompt": "user-turn",
        "task_context": "user-turn",
    }
    assert f"# Reference: {file_ref.as_posix()}" in preview.resolved_request.prompt
    assert f"# Reference: {dir_ref.as_posix()}/" in preview.resolved_request.prompt
    assert "# Meridian Agents" not in preview.resolved_request.prompt
    assert "# Meridian Agents" in preview.projected_content.system_prompt


def test_spawn_prepare_profile_bundle_uses_bundle_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    write_skill(
        tmp_path,
        "verification",
        body="Local skill body should not be composed in bundle mode.",
        description="Verification helper",
    )
    write_agent(
        tmp_path,
        name="dev-orchestrator",
        model="gpt-5.4",
        skills=("verification",),
    )

    bundle = _mock_bundle_for_context()
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: bundle,
    )

    preview = build_launch_context(
        spawn_id="dry-run-bundle-system",
        request=SpawnRequest(
            prompt="bundle task",
            prompt_is_composed=False,
            model="gpt-5.4",
            harness="codex",
            agent="dev-orchestrator",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert preview.projected_content is not None
    assert "## Bundle System" in preview.projected_content.system_prompt
    assert "Local skill body should not be composed" not in preview.projected_content.system_prompt
    assert preview.resolved_request.agent == "dev-orchestrator"
    assert preview.resolved_request.agent_metadata.get("session_agent") == "dev-orchestrator"
    assert preview.resolved_request.launch_bundle_provenance == {"model_source": "mars"}
    assert preview.resolved_request.launch_bundle_warnings == ("bundle warning",)
    assert preview.resolved_request.agent_metadata.get(
        "launch_bundle_provenance.model_source"
    ) == "mars"
    assert preview.resolved_request.agent_metadata.get("launch_bundle.version") == "1"
    assert "bundle warning" in (preview.resolved_request.warning or "")
    assert preview.resolved_request.skill_paths == ()
    assert preview.resolved_request.mcp_tools == ("github=gh",)
    appended = preview.binding.run_params.appended_system_prompt or ""
    assert "Local skill body should not be composed" not in appended
    assert preview.resolved_request.tools is None


def test_spawn_prepare_codex_bundle_tools_do_not_infer_sandbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    write_agent(
        tmp_path,
        name="dev-orchestrator",
        model="gpt-5.4",
    )

    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: _mock_bundle_without_sandbox_with_codex_tools(),
    )

    preview = build_launch_context(
        spawn_id="dry-run-bundle-codex-tools-no-sandbox",
        request=SpawnRequest(
            prompt="bundle task",
            prompt_is_composed=False,
            model="gpt-5.4",
            harness="codex",
            agent="dev-orchestrator",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert preview.resolved_request.tools is None
    assert preview.binding.permission_config.sandbox == "default"
    assert "--sandbox" not in preview.binding.argv


def test_spawn_prepare_bundle_harness_model_routes_to_opencode_command_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    write_agent(
        tmp_path,
        name="dev-orchestrator",
        model="gpt-5.5",
    )
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: _mock_bundle_with_opencode_harness_model(),
    )

    preview = build_launch_context(
        spawn_id="dry-run-bundle-opencode-harness-model",
        request=SpawnRequest(
            prompt="bundle task",
            prompt_is_composed=False,
            model="gpt-5.5",
            harness="opencode",
            agent="dev-orchestrator",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    argv = preview.binding.argv
    assert "--model" in argv
    model_flag_index = argv.index("--model")
    assert argv[model_flag_index + 1] == "openai/gpt-5.5"
    assert preview.resolved_request.model == "gpt-5.5"
    assert preview.resolved_request.model_selection_selected_token == "gpt55"
    assert preview.resolved_request.model_selection_harness_model_id == "openai/gpt-5.5"
    assert preview.resolved_request.model_selection_harness_model_source == "candidate-path"
    assert preview.resolved_request.model_selection_harness_model_confidence == "high"


def test_direct_rebuild_uses_persisted_harness_model_id_for_opencode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    write_agent(
        tmp_path,
        name="dev-orchestrator",
        model="gpt-5.5",
    )
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: _mock_bundle_with_opencode_harness_model(),
    )

    prepared = build_launch_context(
        spawn_id="dry-run-bundle-opencode-persisted",
        request=SpawnRequest(
            prompt="bundle task",
            prompt_is_composed=False,
            model="gpt-5.5",
            harness="opencode",
            agent="dev-orchestrator",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )
    persisted_request = SpawnRequest.model_validate_json(
        prepared.resolved_request.model_dump_json()
    )

    rebuilt = build_launch_context(
        spawn_id="direct-rebuild-bundle-opencode-persisted",
        request=persisted_request,
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.DIRECT,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=False,
    )

    argv = rebuilt.binding.argv
    assert "--model" in argv
    model_flag_index = argv.index("--model")
    assert argv[model_flag_index + 1] == "openai/gpt-5.5"
    assert rebuilt.resolved_request.model == "gpt-5.5"
    assert rebuilt.model_selection is not None
    assert rebuilt.model_selection.selected_model_token == "gpt55"
    assert rebuilt.model_selection.harness_model_id == "openai/gpt-5.5"
    assert rebuilt.model_selection.harness_model_source == "candidate-path"
    assert rebuilt.model_selection.harness_model_confidence == "high"


def test_spawn_prepare_bundle_claude_preserves_native_tool_keys_in_flags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    write_agent(
        tmp_path,
        name="dev-orchestrator",
        model="claude-sonnet-4-5",
    )
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: _mock_bundle_with_claude_native_tool_keys(),
    )

    preview = build_launch_context(
        spawn_id="dry-run-bundle-claude-native-tools",
        request=SpawnRequest(
            prompt="bundle task",
            prompt_is_composed=False,
            model="claude-sonnet-4-5",
            harness="claude",
            agent="dev-orchestrator",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    allowed_flag_index = preview.binding.argv.index("--allowedTools")
    assert (
        preview.binding.argv[allowed_flag_index + 1]
        == "Bash(grep -R),mcp__github__create_issue"
    )
    disallowed_flag_index = preview.binding.argv.index("--disallowedTools")
    assert "Write(File)" in preview.binding.argv[disallowed_flag_index + 1]


def test_spawn_prepare_bundle_claude_skips_abstract_nested_deny_synthesis(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    write_agent(
        tmp_path,
        name="dev-orchestrator",
        model="claude-sonnet-4-5",
    )
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: _mock_bundle_with_claude_native_delegation_allow_tools(),
    )

    preview = build_launch_context(
        spawn_id="dry-run-bundle-claude-native-delegation-tools",
        request=SpawnRequest(
            prompt="bundle task",
            prompt_is_composed=False,
            model="claude-sonnet-4-5",
            harness="claude",
            agent="dev-orchestrator",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert preview.resolved_request.tools == {
        "Agent": "allow",
        "TaskCreate": "allow",
        "TaskGet": "allow",
        "TaskList": "allow",
        "TaskOutput": "allow",
        "TaskStop": "allow",
        "TaskUpdate": "allow",
        "mcp__github__create_issue": "allow",
    }
    assert "agent" not in preview.resolved_request.tools
    assert "task" not in preview.resolved_request.tools
    allowed_flag_index = preview.binding.argv.index("--allowedTools")
    assert (
        preview.binding.argv[allowed_flag_index + 1]
        == (
            "Agent,TaskCreate,TaskGet,TaskList,TaskOutput,TaskStop,TaskUpdate,"
            "mcp__github__create_issue"
        )
    )


def test_spawn_prepare_bundle_claude_partial_delegation_policy_still_applies_fallback_deny(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    write_agent(
        tmp_path,
        name="dev-orchestrator",
        model="claude-sonnet-4-5",
    )
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: _mock_bundle_with_claude_partial_native_delegation_tools(),
    )

    preview = build_launch_context(
        spawn_id="dry-run-bundle-claude-partial-native-delegation-tools",
        request=SpawnRequest(
            prompt="bundle task",
            prompt_is_composed=False,
            model="claude-sonnet-4-5",
            harness="claude",
            agent="dev-orchestrator",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert preview.resolved_request.tools == {
        "Agent": "allow",
        "TaskCreate": "allow",
        "mcp__github__create_issue": "allow",
        "TaskGet": "deny",
        "TaskList": "deny",
        "TaskOutput": "deny",
        "TaskStop": "deny",
        "TaskUpdate": "deny",
    }
    disallowed_flag_index = preview.binding.argv.index("--disallowedTools")
    disallowed = set(preview.binding.argv[disallowed_flag_index + 1].split(","))
    assert "TaskCreate" not in disallowed
    assert {"TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate"} <= disallowed


def test_direct_rebuild_preserves_claude_partial_native_delegation_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    write_agent(
        tmp_path,
        name="dev-orchestrator",
        model="claude-sonnet-4-5",
    )
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: _mock_bundle_with_claude_partial_native_delegation_tools(),
    )

    prepared = build_launch_context(
        spawn_id="dry-run-bundle-claude-partial-native-delegation-direct-source",
        request=SpawnRequest(
            prompt="bundle task",
            prompt_is_composed=False,
            model="claude-sonnet-4-5",
            harness="claude",
            agent="dev-orchestrator",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )
    persisted_request = SpawnRequest.model_validate_json(
        prepared.resolved_request.model_dump_json()
    )

    rebuilt = build_launch_context(
        spawn_id="direct-rebuild-bundle-claude-partial-native-delegation",
        request=persisted_request,
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.DIRECT,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=False,
    )

    assert rebuilt.resolved_request.tools == prepared.resolved_request.tools
    assert "agent" not in rebuilt.resolved_request.tools
    assert "task" not in rebuilt.resolved_request.tools
    assert rebuilt.resolved_request.tools["Agent"] == "allow"
    assert rebuilt.resolved_request.tools["TaskCreate"] == "allow"
    for task_tool in ("TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate"):
        assert rebuilt.resolved_request.tools[task_tool] == "deny"

    allowed_flag_index = rebuilt.binding.argv.index("--allowedTools")
    allowed = set(rebuilt.binding.argv[allowed_flag_index + 1].split(","))
    assert {"Agent", "TaskCreate", "mcp__github__create_issue"} <= allowed
    disallowed_flag_index = rebuilt.binding.argv.index("--disallowedTools")
    disallowed = set(rebuilt.binding.argv[disallowed_flag_index + 1].split(","))
    assert "TaskCreate" not in disallowed
    assert {"TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate"} <= disallowed


def test_spawn_prepare_bundle_claude_without_delegation_policy_applies_fallback_deny(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    write_agent(
        tmp_path,
        name="dev-orchestrator",
        model="claude-sonnet-4-5",
    )
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: _mock_bundle_with_claude_no_delegation_policy_tools(),
    )

    preview = build_launch_context(
        spawn_id="dry-run-bundle-claude-no-delegation-policy",
        request=SpawnRequest(
            prompt="bundle task",
            prompt_is_composed=False,
            model="claude-sonnet-4-5",
            harness="claude",
            agent="dev-orchestrator",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert preview.resolved_request.tools == {"*": "allow", "agent": "deny", "task": "deny"}
    disallowed_flag_index = preview.binding.argv.index("--disallowedTools")
    disallowed = set(preview.binding.argv[disallowed_flag_index + 1].split(","))
    assert {
        "Agent",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskOutput",
        "TaskStop",
        "TaskUpdate",
    } <= disallowed


def test_direct_rebuild_preserves_claude_abstract_delegation_fallback_deny(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    write_agent(
        tmp_path,
        name="dev-orchestrator",
        model="claude-sonnet-4-5",
    )
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: _mock_bundle_with_claude_no_delegation_policy_tools(),
    )

    prepared = build_launch_context(
        spawn_id="dry-run-bundle-claude-abstract-delegation-direct-source",
        request=SpawnRequest(
            prompt="bundle task",
            prompt_is_composed=False,
            model="claude-sonnet-4-5",
            harness="claude",
            agent="dev-orchestrator",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )
    persisted_request = SpawnRequest.model_validate_json(
        prepared.resolved_request.model_dump_json()
    )

    rebuilt = build_launch_context(
        spawn_id="direct-rebuild-bundle-claude-abstract-delegation",
        request=persisted_request,
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.DIRECT,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=False,
    )

    assert rebuilt.resolved_request.tools == {"*": "allow", "agent": "deny", "task": "deny"}
    assert "--allowedTools" not in rebuilt.binding.argv
    disallowed_flag_index = rebuilt.binding.argv.index("--disallowedTools")
    disallowed = set(rebuilt.binding.argv[disallowed_flag_index + 1].split(","))
    assert {
        "Agent",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskOutput",
        "TaskStop",
        "TaskUpdate",
    } <= disallowed


@pytest.mark.parametrize(
    ("harness", "model"),
    [
        ("codex", "gpt-5.4"),
        ("opencode", "kimi-k2.6"),
    ],
)
def test_spawn_prepare_system_field_harnesses_route_agent_inventory_to_system_prompt(
    tmp_path: Path,
    harness: str,
    model: str,
) -> None:
    _write_minimal_mars_config(tmp_path)
    write_agent(tmp_path, name="dev-orchestrator", model="claude-sonnet-4-5")
    write_agent(tmp_path, name="reviewer", model="gpt-5.4")

    preview = build_launch_context(
        spawn_id=f"dry-run-{harness}-spawn-prepare-no-inventory",
        request=SpawnRequest(
            prompt="task prompt",
            prompt_is_composed=False,
            model=model,
            harness=harness,
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert preview.projected_content is not None
    inventory_channel = preview.projected_content.system_prompt
    assert "# Meridian Agents" in inventory_channel
    assert "## Subagent" in inventory_channel
    assert "- dev-orchestrator" in inventory_channel
    assert "- reviewer" in inventory_channel
    assert "# Meridian Agents" not in preview.projected_content.user_turn_content
    assert "# Meridian Agents" not in preview.resolved_request.prompt


def test_spawn_prepare_claude_projects_skills_inventory_and_report_to_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _force_legacy_profile_path(monkeypatch)
    _write_minimal_mars_config(tmp_path)
    write_skill(
        tmp_path,
        "verification",
        body="Use verification checklist.",
        description="Verification helper",
    )
    write_agent(
        tmp_path,
        name="dev-orchestrator",
        model="claude-sonnet-4-5",
        skills=("verification",),
    )
    write_agent(tmp_path, name="reviewer", model="gpt-5.4")
    file_ref = tmp_path / "README.md"
    file_ref.write_text("# project\n", encoding="utf-8")

    preview = build_launch_context(
        spawn_id="dry-run-claude-spawn-prepare",
        request=SpawnRequest(
            prompt="complete the task",
            prompt_is_composed=False,
            model="claude-sonnet-4-5",
            harness="claude",
            agent="dev-orchestrator",
            reference_files=(file_ref.as_posix(),),
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert preview.projected_content is not None
    projected = preview.projected_content

    assert preview.resolved_request.skill_paths

    # Claude declares supports_native_skills=True, so skill content is
    # suppressed from supplemental_documents (projected.system_prompt).
    # Skills are delivered via compose_skill_injections() → appended_system_prompt.
    assert "# Skill:" not in projected.system_prompt
    assert "# Meridian Agents" in projected.system_prompt
    assert "## Subagent" in projected.system_prompt
    assert "- dev-orchestrator" in projected.system_prompt
    assert "- reviewer" in projected.system_prompt
    assert "# Report" in projected.system_prompt
    assert "final assistant message must be the run report" in projected.system_prompt

    # Skills still delivered via --append-system-prompt for Claude.
    # The argv uses --append-system-prompt-file, so skill content is in the
    # system prompt file content, not directly in argv.
    assert any("--append-system-prompt-file" in str(arg) for arg in preview.binding.argv)
    # Verify skill content is actually in the appended payload, not just the flag.
    assert preview.binding.run_params.appended_system_prompt is not None
    assert "Use verification checklist." in preview.binding.run_params.appended_system_prompt

    assert "complete the task" in projected.user_turn_content
    assert f"# Reference: {file_ref.as_posix()}" in projected.user_turn_content
    assert "# Skill:" not in projected.user_turn_content
    assert "# Meridian Agents" not in projected.user_turn_content

    assert preview.binding.run_params.prompt == projected.user_turn_content
    assert "# Skill:" not in preview.binding.run_params.prompt
    assert "# Meridian Agents" not in preview.binding.run_params.prompt


def test_spawn_prepare_claude_continue_session_keeps_skills_in_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _force_legacy_profile_path(monkeypatch)
    _write_minimal_mars_config(tmp_path)
    write_skill(
        tmp_path,
        "verification",
        body="Use verification checklist.",
        description="Verification helper",
    )
    write_agent(
        tmp_path,
        name="dev-orchestrator",
        model="claude-sonnet-4-5",
        skills=("verification",),
    )
    write_agent(tmp_path, name="reviewer", model="gpt-5.4")

    harness_session_id = "claude-session-123"
    preview = build_launch_context(
        spawn_id="dry-run-claude-spawn-prepare-continue",
        request=SpawnRequest(
            prompt="continue the task",
            prompt_is_composed=False,
            model="claude-sonnet-4-5",
            harness="claude",
            agent="dev-orchestrator",
            session=SessionRequest(
                requested_harness_session_id=harness_session_id,
                continue_harness="claude",
                continue_fork=True,
            ),
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert preview.projected_content is not None
    projected = preview.projected_content

    assert preview.binding.run_params.continue_harness_session_id == harness_session_id
    assert preview.binding.run_params.continue_fork is True
    # Claude declares supports_native_skills=True, so skill content is
    # suppressed from supplemental_documents (projected.system_prompt).
    # Skills are delivered via compose_skill_injections() → appended_system_prompt.
    assert "# Skill:" not in projected.system_prompt
    assert "# Meridian Agents" in projected.system_prompt
    assert "# Report" in projected.system_prompt
    # Skills still delivered via --append-system-prompt-file
    assert any("--append-system-prompt-file" in str(arg) for arg in preview.binding.argv)
    assert preview.binding.run_params.appended_system_prompt is not None
    assert "Use verification checklist." in preview.binding.run_params.appended_system_prompt

    assert "continue the task" in projected.user_turn_content
    assert "# Skill:" not in projected.user_turn_content
    assert "# Meridian Agents" not in projected.user_turn_content
