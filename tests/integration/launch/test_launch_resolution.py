import json
from pathlib import Path

import pytest

from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.harness.workspace_projection import OPENCODE_CONFIG_CONTENT_ENV
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.launch_types import TerminalSurfaceMode
from meridian.lib.launch.plan import (
    build_primary_launch_runtime,
    build_primary_spawn_request,
)
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from meridian.lib.launch.types import LaunchRequest
from meridian.plugin_api.git import resolve_clone_path
from tests.support.fixtures import write_agent, write_skill

pytestmark = pytest.mark.slow


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        "[settings]\n"
        'targets = [".claude"]\n',
        encoding="utf-8",
    )


def test_build_primary_launch_runtime_preserves_execution_cwd(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    execution_cwd = project_root / "subdir"
    execution_cwd.mkdir(parents=True)

    runtime = build_primary_launch_runtime(
        project_root=project_root,
        execution_cwd=execution_cwd,
    )

    assert runtime.project_paths_project_root == project_root.resolve().as_posix()
    assert runtime.project_paths_execution_cwd == execution_cwd.resolve().as_posix()


@pytest.mark.parametrize(
    ("model", "peer_name", "peer_model", "skill_name", "skill_description"),
    [
        (
            "claude-sonnet-4",
            "coder",
            "gpt-5.4",
            "review",
            "Review helper",
        ),
        (
            "gpt-5.4",
            "reviewer",
            "claude-sonnet-4",
            "meridian-spawn",
            "Spawn helper",
        ),
        (
            "gemini-2.5-pro",
            "smoke-tester",
            "claude-sonnet-4",
            "verification",
            "Verification helper",
        ),
    ],
    ids=["claude", "codex", "opencode"],
)
def test_primary_launch_injects_inventory_by_harness_family(
    tmp_path: Path,
    model: str,
    peer_name: str,
    peer_model: str,
    skill_name: str,
    skill_description: str,
) -> None:
    _write_minimal_mars_config(tmp_path)
    write_agent(tmp_path, name="dev-orchestrator", model=model)
    write_agent(tmp_path, name=peer_name, model=peer_model)
    write_skill(tmp_path, skill_name, description=skill_description)
    registry = get_default_harness_registry()

    preview = build_launch_context(
        spawn_id="dry-run-primary",
        request=build_primary_spawn_request(
            request=LaunchRequest(model=model, agent="dev-orchestrator")
        ),
        runtime=build_primary_launch_runtime(project_root=tmp_path),
        harness_registry=registry,
        dry_run=True,
    )

    text = (
        preview.projected_content.system_prompt
        if preview.projected_content and preview.projected_content.system_prompt
        else preview.run_params.prompt
    )
    assert "# Meridian Agents" in text
    assert "## Subagent" in text
    assert "- dev-orchestrator" in text
    assert f"- {peer_name}" in text
    assert "SKILLS" not in text
    assert f"{skill_name}: {skill_description}" not in text


@pytest.mark.parametrize(
    ("model", "expected_harness"),
    [
        ("claude-sonnet-4", HarnessId.CLAUDE),
        ("gpt-5.4", HarnessId.CODEX),
        ("gemini-2.5-pro", HarnessId.OPENCODE),
    ],
    ids=["claude", "codex", "opencode"],
)
def test_launch_policy_terminal_surface_mode_defaults_to_pty_mediated(
    tmp_path: Path,
    model: str,
    expected_harness: HarnessId,
) -> None:
    _write_minimal_mars_config(tmp_path)
    write_agent(tmp_path, name="dev-orchestrator", model=model)

    preview = build_launch_context(
        spawn_id=f"dry-run-{expected_harness.value}-terminal-surface",
        request=build_primary_spawn_request(
            request=LaunchRequest(model=model, agent="dev-orchestrator")
        ),
        runtime=build_primary_launch_runtime(project_root=tmp_path),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert preview.harness.id is expected_harness
    assert preview.resolved_request.terminal_surface_mode is TerminalSurfaceMode.PTY_MEDIATED


def test_launch_skill_variants_use_alias_then_canonical_then_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _resolve_model_for_alias_variant(
        self: CatalogSession,
        token: str,
    ) -> AliasEntry:
        _ = (self, token)
        return AliasEntry(
            alias="alias-token",
            model_id=ModelId("canonical-id"),
            resolved_harness=HarnessId.CODEX,
        )

    monkeypatch.setattr(
        CatalogSession,
        "resolve_model",
        _resolve_model_for_alias_variant,
    )
    _write_minimal_mars_config(tmp_path)
    write_agent(
        tmp_path,
        name="dev-orchestrator",
        model="alias-token",
        skills=["variant-skill"],
    )
    write_skill(tmp_path, "variant-skill", body="Base body", description="Base metadata")
    skill_root = tmp_path / ".mars" / "skills" / "variant-skill"
    token_variant = skill_root / "variants" / "codex" / "alias-token" / "SKILL.md"
    token_variant.parent.mkdir(parents=True)
    token_variant.write_text(
        "---\nname: ignored-token\ndescription: ignored\n---\n\nAlias token body",
        encoding="utf-8",
    )
    canonical_variant = skill_root / "variants" / "codex" / "canonical-id" / "SKILL.md"
    canonical_variant.parent.mkdir(parents=True)
    canonical_variant.write_text("Canonical body", encoding="utf-8")
    harness_variant = skill_root / "variants" / "codex" / "SKILL.md"
    harness_variant.write_text("Harness body", encoding="utf-8")

    preview = build_launch_context(
        spawn_id="dry-run-variant-alias",
        request=build_primary_spawn_request(
            request=LaunchRequest(model="alias-token", agent="dev-orchestrator")
        ),
        runtime=build_primary_launch_runtime(project_root=tmp_path),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    system_prompt = preview.projected_content.system_prompt if preview.projected_content else ""
    assert "Alias token body" in system_prompt
    assert "Canonical body" not in system_prompt
    assert "Harness body" not in system_prompt
    assert "name: variant-skill" in system_prompt
    assert "description: Base metadata" in system_prompt
    assert "name: ignored-token" not in system_prompt
    assert "# Skill: " + token_variant.resolve().as_posix() in system_prompt
    assert preview.resolved_request.skill_paths == (token_variant.resolve().as_posix(),)


def test_launch_skill_variants_fall_back_to_canonical_then_harness_and_exact_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _resolve_model_for_fallback_variant(
        self: CatalogSession,
        token: str,
    ) -> AliasEntry:
        _ = (self, token)
        return AliasEntry(
            alias="alias-token",
            model_id=ModelId("canonical-id"),
            resolved_harness=HarnessId.CODEX,
        )

    monkeypatch.setattr(
        CatalogSession,
        "resolve_model",
        _resolve_model_for_fallback_variant,
    )
    _write_minimal_mars_config(tmp_path)
    write_agent(
        tmp_path,
        name="dev-orchestrator",
        model="alias-token",
        skills=["variant-skill"],
    )
    write_skill(tmp_path, "variant-skill", body="Base body")
    skill_root = tmp_path / ".mars" / "skills" / "variant-skill"
    prefix_variant = skill_root / "variants" / "codex" / "alias" / "SKILL.md"
    prefix_variant.parent.mkdir(parents=True)
    prefix_variant.write_text("Prefix body", encoding="utf-8")
    canonical_variant = skill_root / "variants" / "codex" / "canonical-id" / "SKILL.md"
    canonical_variant.parent.mkdir(parents=True)
    canonical_variant.write_text("Canonical body", encoding="utf-8")
    harness_variant = skill_root / "variants" / "codex" / "SKILL.md"
    harness_variant.write_text("Harness body", encoding="utf-8")

    canonical_preview = build_launch_context(
        spawn_id="dry-run-variant-canonical",
        request=build_primary_spawn_request(
            request=LaunchRequest(model="alias-token", agent="dev-orchestrator")
        ),
        runtime=build_primary_launch_runtime(project_root=tmp_path),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )
    canonical_prompt = (
        canonical_preview.projected_content.system_prompt
        if canonical_preview.projected_content
        else ""
    )
    assert "Canonical body" in canonical_prompt
    assert "Prefix body" not in canonical_prompt

    canonical_variant.unlink()
    harness_preview = build_launch_context(
        spawn_id="dry-run-variant-harness",
        request=build_primary_spawn_request(
            request=LaunchRequest(model="alias-token", agent="dev-orchestrator")
        ),
        runtime=build_primary_launch_runtime(project_root=tmp_path),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )
    harness_prompt = (
        harness_preview.projected_content.system_prompt if harness_preview.projected_content else ""
    )
    assert "Harness body" in harness_prompt
    assert "Prefix body" not in harness_prompt


def test_workspace_roots_append_after_claude_preflight_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    shared_root = tmp_path / "shared"
    shared_root.mkdir()
    (tmp_path / "meridian.local.toml").write_text(
        "[workspace.shared]\n"
        'path = "./shared"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDECODE", "1")
    registry = get_default_harness_registry()

    preview = build_launch_context(
        spawn_id="dry-run-claude-workspace-order",
        request=SpawnRequest(
            prompt="workspace order",
            model="claude-sonnet-4-5",
            harness="claude",
            extra_args=("--user-tail", "1"),
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=registry,
        dry_run=True,
    )

    runtime_root = tmp_path / ".meridian"
    assert preview.child_cwd == tmp_path
    args = preview.run_params.extra_args
    assert args[:2] == ("--user-tail", "1")
    assert "--add-dir" not in args
    projected_roots = {path.as_posix() for path in preview.spec.projected_roots}
    assert shared_root.as_posix() in projected_roots
    assert runtime_root.as_posix() in projected_roots
    assert shared_root.as_posix() in preview.argv
    assert runtime_root.as_posix() in preview.argv


def test_named_workspace_roots_project_through_codex_launch_context(tmp_path: Path) -> None:
    _write_minimal_mars_config(tmp_path)
    committed_root = tmp_path / "committed-root"
    local_override_root = tmp_path / "local-override-root"
    local_only_root = tmp_path / "local-only-root"
    for path in (committed_root, local_override_root, local_only_root):
        path.mkdir()
    (tmp_path / "meridian.toml").write_text(
        "[workspace.shared]\n"
        'path = "./committed-root"\n'
        "\n"
        "[workspace.local_only]\n"
        'path = "./local-only-root"\n',
        encoding="utf-8",
    )
    (tmp_path / "meridian.local.toml").write_text(
        "[workspace.shared]\n"
        'path = "./local-override-root"\n'
        "\n"
        "[workspace.local_only]\n"
        'path = "./local-only-root"\n',
        encoding="utf-8",
    )

    preview = build_launch_context(
        spawn_id="dry-run-codex-named-workspace",
        request=SpawnRequest(
            prompt="workspace projection",
            model="gpt-5.4",
            harness="codex",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    runtime_root = tmp_path / ".meridian"
    assert "--add-dir" not in preview.run_params.extra_args
    projected_roots = {path.as_posix() for path in preview.spec.projected_roots}
    assert local_override_root.as_posix() in projected_roots
    assert local_only_root.as_posix() in projected_roots
    assert runtime_root.as_posix() in projected_roots
    codex_add_dir_pairs = {
        (preview.argv[index], preview.argv[index + 1])
        for index, token in enumerate(preview.argv[:-1])
        if token == "--add-dir"
    }
    assert ("--add-dir", local_override_root.as_posix()) in codex_add_dir_pairs
    assert ("--add-dir", local_only_root.as_posix()) in codex_add_dir_pairs
    assert ("--add-dir", runtime_root.as_posix()) in codex_add_dir_pairs


def test_git_backed_context_remote_projects_clone_root_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    remote = "git@github.com:meridian-flow/docs.git"
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "user-home").as_posix())
    (tmp_path / "meridian.toml").write_text(
        "[context.work]\n"
        'source = "git"\n'
        f'remote = "{remote}"\n'
        'path = "work"\n'
        "\n"
        "[context.kb]\n"
        'source = "git"\n'
        f'remote = "{remote}"\n'
        'path = "kb"\n',
        encoding="utf-8",
    )

    registry = get_default_harness_registry()

    preview = build_launch_context(
        spawn_id="dry-run-codex-git-context-root",
        request=SpawnRequest(
            prompt="workspace projection",
            model="gpt-5.4",
            harness="codex",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=registry,
        dry_run=True,
    )

    clone_root = resolve_clone_path(remote)
    runtime_root = tmp_path / ".meridian"
    projected_root_values = [path.as_posix() for path in preview.spec.projected_roots]
    assert projected_root_values.count(clone_root.as_posix()) == 1
    assert projected_root_values.count(runtime_root.as_posix()) == 1

    monkeypatch.setenv("CLAUDECODE", "1")
    claude_preview = build_launch_context(
        spawn_id="dry-run-claude-git-context-root",
        request=SpawnRequest(
            prompt="workspace projection",
            model="claude-sonnet-4-5",
            harness="claude",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            runtime_root=runtime_root.as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=registry,
        dry_run=True,
    )
    claude_clone_root_pairs = sum(
        1
        for index, token in enumerate(claude_preview.argv[:-1])
        if token == "--add-dir" and claude_preview.argv[index + 1] == clone_root.as_posix()
    )
    assert claude_clone_root_pairs == 1


def test_named_workspace_roots_project_through_opencode_launch_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Isolate from parent OpenCode session env for deterministic assertions.
    monkeypatch.delenv(OPENCODE_CONFIG_CONTENT_ENV, raising=False)
    _write_minimal_mars_config(tmp_path)
    docs_root = tmp_path / "docs-root"
    docs_root.mkdir()
    (tmp_path / "meridian.toml").write_text(
        "[workspace.docs]\n"
        'path = "./docs-root"\n',
        encoding="utf-8",
    )

    preview = build_launch_context(
        spawn_id="dry-run-opencode-named-workspace",
        request=SpawnRequest(
            prompt="workspace projection",
            model="gemini-2.5-pro",
            harness="opencode",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    runtime_root = tmp_path / ".meridian"
    payload = json.loads(preview.env_overrides[OPENCODE_CONFIG_CONTENT_ENV])
    external_dirs = payload["permission"]["external_directory"]
    assert external_dirs[docs_root.as_posix() + "/**"] == "allow"
    assert external_dirs[runtime_root.as_posix() + "/**"] == "allow"


@pytest.mark.parametrize(
    "parent_env_present",
    [False, True],
    ids=["without_parent_env", "with_parent_env"],
)
def test_opencode_workspace_projection_merges_parent_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_env_present: bool,
) -> None:
    _write_minimal_mars_config(tmp_path)
    shared_root = tmp_path / "shared"
    shared_root.mkdir()
    (tmp_path / "meridian.local.toml").write_text(
        "[workspace.shared]\n"
        'path = "./shared"\n',
        encoding="utf-8",
    )
    if parent_env_present:
        monkeypatch.setenv(
            OPENCODE_CONFIG_CONTENT_ENV,
            '{"instructions":["/tmp/system.md"],'
            '"permission":{"external_directory":{"/existing/*":"ask"}}}',
        )
    else:
        monkeypatch.delenv(OPENCODE_CONFIG_CONTENT_ENV, raising=False)
    registry = get_default_harness_registry()

    preview = build_launch_context(
        spawn_id="dry-run-opencode-workspace-suppressed",
        request=SpawnRequest(
            prompt="workspace projection",
            model="gemini-2.5-pro",
            harness="opencode",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=registry,
        dry_run=True,
    )

    warning_codes = {warning.code for warning in preview.warnings}
    runtime_root = tmp_path / ".meridian"
    payload = json.loads(preview.env_overrides[OPENCODE_CONFIG_CONTENT_ENV])
    external_dirs = payload["permission"]["external_directory"]
    assert external_dirs[shared_root.as_posix() + "/**"] == "allow"
    assert external_dirs[runtime_root.as_posix() + "/**"] == "allow"
    if parent_env_present:
        assert payload["instructions"] == ["/tmp/system.md"]
        assert external_dirs["/existing/*"] == "ask"
    assert "workspace_opencode_parent_env_suppressed" not in warning_codes


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
            model="gemini-2.5-pro",
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

    assert "--file" not in preview.argv
    assert file_ref.as_posix() not in preview.argv
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


@pytest.mark.parametrize(
    ("harness", "model"),
    [
        ("codex", "gpt-5.4"),
        ("opencode", "gemini-2.5-pro"),
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
    tmp_path: Path,
) -> None:
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
    skill_path = preview.resolved_request.skill_paths[0]

    assert f"# Skill: {skill_path}" in projected.system_prompt
    assert "Use verification checklist." in projected.system_prompt
    assert "# Meridian Agents" in projected.system_prompt
    assert "## Subagent" in projected.system_prompt
    assert "- dev-orchestrator" in projected.system_prompt
    assert "- reviewer" in projected.system_prompt
    assert "# Report" in projected.system_prompt
    assert "final assistant message must be the run report" in projected.system_prompt

    assert "complete the task" in projected.user_turn_content
    assert f"# Reference: {file_ref.as_posix()}" in projected.user_turn_content
    assert "# Skill:" not in projected.user_turn_content
    assert "# Meridian Agents" not in projected.user_turn_content

    assert preview.run_params.prompt == projected.user_turn_content
    assert "# Skill:" not in preview.run_params.prompt
    assert "# Meridian Agents" not in preview.run_params.prompt


def test_spawn_prepare_claude_continue_session_keeps_skills_in_system_prompt(
    tmp_path: Path,
) -> None:
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

    assert preview.run_params.continue_harness_session_id == harness_session_id
    assert preview.run_params.continue_fork is True
    assert "Use verification checklist." in projected.system_prompt
    assert "# Meridian Agents" in projected.system_prompt
    assert "# Report" in projected.system_prompt

    assert "continue the task" in projected.user_turn_content
    assert "# Skill:" not in projected.user_turn_content
    assert "# Meridian Agents" not in projected.user_turn_content
