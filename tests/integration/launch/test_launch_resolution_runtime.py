# qa-validated: test-suite-redesign
"""Runtime building, spawn request construction, and terminal surface mode tests."""

from __future__ import annotations

from pathlib import Path

import pytest

import meridian.lib.harness.cursor as cursor_harness
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import (
    HarnessRegistry,
    get_default_harness_registry,
)
from meridian.lib.launch import LaunchRequest as PrimaryLaunchRequest
from meridian.lib.launch import launch_primary
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.launch_types import ResolvedExecutionPolicy, TerminalSurfaceMode
from meridian.lib.launch.plan import (
    build_primary_launch_runtime,
    build_primary_spawn_request,
)
from meridian.lib.launch.request import LaunchCompositionSurface
from meridian.lib.launch.types import LaunchRequest, build_primary_prompt
from meridian.lib.ops.spawn import context_ref
from meridian.lib.state import work_store
from tests.support.fixtures import write_agent
from tests.support.launch import stub_bundle_request_and_resolve

pytestmark = pytest.mark.slow


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )


def _write_agent_profile(project_root: Path, *, name: str, frontmatter: str) -> None:
    path = project_root / ".mars" / "agents" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n\n# {name}\n", encoding="utf-8")


def _registry_with_harnesses(*harness_ids: HarnessId) -> HarnessRegistry:
    base_registry = get_default_harness_registry()
    registry = HarnessRegistry()
    for harness_id in harness_ids:
        registry.register(base_registry.get(harness_id))
    return registry


def test_build_primary_launch_runtime_preserves_execution_cwd(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    execution_cwd = project_root / "subdir"
    execution_cwd.mkdir(parents=True)

    runtime = build_primary_launch_runtime(
        project_root=project_root,
        execution_cwd=execution_cwd,
    )

    assert runtime.config_root == project_root.resolve().as_posix()
    assert runtime.control_root == project_root.resolve().as_posix()
    assert runtime.requested_task_cwd == execution_cwd.resolve().as_posix()
    assert runtime.project_paths_project_root == project_root.resolve().as_posix()
    assert runtime.project_paths_execution_cwd == execution_cwd.resolve().as_posix()


@pytest.mark.parametrize(
    ("harness", "requires_initial_prompt"),
    [
        (HarnessId.CLAUDE, False),
        (HarnessId.CODEX, True),
    ],
    ids=["claude", "codex"],
)
def test_build_primary_spawn_request_only_uses_synthetic_prompt_when_required(
    harness: HarnessId,
    requires_initial_prompt: bool,
) -> None:
    request = LaunchRequest(
        model="test-model",
        harness=harness.value,
    )

    spawn_request = build_primary_spawn_request(request=request)

    if requires_initial_prompt:
        assert spawn_request.prompt == build_primary_prompt(request)
        assert spawn_request.primary_prompt_is_synthetic is True
    else:
        assert spawn_request.prompt == ""
        assert spawn_request.primary_prompt_is_synthetic is False


def test_build_primary_spawn_request_uses_context_as_initial_prompt() -> None:
    request = LaunchRequest(
        model="test-model",
        harness=HarnessId.CODEX.value,
        context_from=("p123",),
    )

    spawn_request = build_primary_spawn_request(request=request)

    assert spawn_request.prompt == ""
    assert spawn_request.primary_prompt_is_synthetic is False


def test_build_primary_spawn_request_copies_context_from() -> None:
    request = LaunchRequest(
        model="test-model",
        harness=HarnessId.CLAUDE.value,
        context_from=("p123",),
    )

    spawn_request = build_primary_spawn_request(request=request)

    assert spawn_request.context_from == ("p123",)


@pytest.mark.parametrize(
    "model",
    ["claude-sonnet-4"],
    ids=["claude-model"],
)
def test_build_primary_spawn_request_without_harness_skips_synthetic_prompt(model: str) -> None:
    """Without explicit harness we default to no synthetic first prompt."""

    request = LaunchRequest(model=model)

    spawn_request = build_primary_spawn_request(request=request)

    assert spawn_request.prompt == ""


def test_primary_launch_injects_bundle_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "claude-sonnet-4"
    peer_name = "coder"
    skill_name = "review"
    skill_description = "Review helper"
    bundle_inventory = (
        "# Meridian Agents\n\n"
        "## Subagent\n"
        "- `meridian spawn -a dev-orchestrator`: Orchestrate.\n"
        f"- `meridian spawn -a {peer_name}`: Peer.\n"
    )
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model=model,
        harness=HarnessId.CLAUDE,
        prompt_surface_inventory_prompt=bundle_inventory,
    )
    write_agent(tmp_path, name="dev-orchestrator", model=model)
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
        else preview.binding.run_params.prompt
    )
    assert "# Meridian Agents" in text
    assert "## Subagent" in text
    assert "`meridian spawn -a dev-orchestrator`" in text
    assert f"`meridian spawn -a {peer_name}`" in text
    assert "SKILLS" not in text
    assert f"{skill_name}: {skill_description}" not in text


def test_primary_projection_places_from_context_in_user_turn_not_system_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-sonnet-4",
        harness=HarnessId.CLAUDE,
    )
    write_agent(tmp_path, name="dev-orchestrator", model="claude-sonnet-4")

    def _resolve_context_ref(_root: Path, _ref: str) -> object:
        return object()

    def _resolved_context_ref_value(_ref: object) -> str:
        return "p999"

    def _render_context_refs(_refs: object) -> str:
        return '<prior-spawn-context spawn="p999">prior context</prior-spawn-context>'

    monkeypatch.setattr(context_ref, "resolve_context_ref", _resolve_context_ref)
    monkeypatch.setattr(context_ref, "resolved_context_ref_value", _resolved_context_ref_value)
    monkeypatch.setattr(
        context_ref,
        "render_context_refs",
        _render_context_refs,
    )

    preview = build_launch_context(
        spawn_id="dry-run-primary-from",
        request=build_primary_spawn_request(
            request=LaunchRequest(
                model="claude-sonnet-4",
                agent="dev-orchestrator",
                context_from=("p123",),
            )
        ),
        runtime=build_primary_launch_runtime(project_root=tmp_path),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert preview.projected_content is not None
    assert "prior context" in preview.projected_content.user_turn_content
    assert "prior context" not in preview.projected_content.system_prompt
    assert preview.resolved_request.context_from == ("p999",)


def test_codex_primary_projection_omits_synthetic_user_turn_without_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4-mini",
        harness=HarnessId.CODEX,
    )

    preview = build_launch_context(
        spawn_id="dry-run-primary-codex-empty",
        request=build_primary_spawn_request(
            request=LaunchRequest(model="gpt-5.4-mini", harness=HarnessId.CODEX.value)
        ),
        runtime=build_primary_launch_runtime(project_root=tmp_path),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert preview.binding.run_params.prompt.strip()
    assert preview.binding.run_params.user_turn_content is None


def test_primary_launch_resolves_reference_files_from_work_task_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    task_dir = tmp_path / "task"
    project_root.mkdir()
    task_dir.mkdir()
    _write_minimal_mars_config(project_root)
    (project_root / "task-only.txt").write_text("project shadow", encoding="utf-8")
    (task_dir / "task-only.txt").write_text("task marker", encoding="utf-8")
    work_store.ensure_work_item_metadata(project_root / ".meridian", "task-work")
    work_store.update_work_item_task_dir(
        project_root / ".meridian",
        "task-work",
        task_dir=task_dir.as_posix(),
    )
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4-mini",
        harness=HarnessId.CODEX,
    )

    result = launch_primary(
        project_root=project_root,
        request=PrimaryLaunchRequest(
            model="gpt-5.4-mini",
            harness=HarnessId.CODEX.value,
            work_id="task-work",
            reference_files=("task-only.txt",),
            dry_run=True,
        ),
        harness_registry=get_default_harness_registry(),
    )

    prompt = result.command[-1]
    assert (task_dir / "task-only.txt").as_posix() in prompt
    assert "task marker" in prompt
    assert "project shadow" not in prompt


def test_primary_launch_invalid_reference_does_not_create_explicit_work(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()

    with pytest.raises(FileNotFoundError):
        launch_primary(
            project_root=project_root,
            request=PrimaryLaunchRequest(
                work_id="new-work",
                reference_files=("missing.txt",),
                dry_run=True,
            ),
            harness_registry=get_default_harness_registry(),
        )

    assert not (project_root / ".meridian" / "work").exists()
    assert not (project_root / ".meridian" / "id").exists()


@pytest.mark.parametrize(
    ("model", "expected_harness"),
    [
        ("claude-sonnet-4", HarnessId.CLAUDE),
    ],
    ids=["claude"],
)
def test_launch_policy_terminal_surface_mode_defaults_to_pty_mediated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    expected_harness: HarnessId,
) -> None:
    # Link all three harness targets so mars's per-provider routing returns
    # the harness the test parametrize expects. The minimal `.claude`-only
    # config (used by other tests in this module) collapses every model to
    # claude under the mars 0.4.8rc3 resolver and would invalidate the
    # cross-harness coverage this test is asserting.
    (tmp_path / "mars.toml").write_text(
        '[settings]\ntargets = [".claude", ".codex", ".opencode"]\n',
        encoding="utf-8",
    )
    stub_bundle_request_and_resolve(
        monkeypatch,
        model=model,
        harness=expected_harness,
    )
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


def test_launch_resolution_fallback_policy_resolves_opencode_medium_via_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="dev-orchestrator",
        frontmatter=(
            "name: dev-orchestrator\n"
            "model: claude\n"
            "model-policies:\n"
            "  - match: {alias: gpt55}\n"
            "    override: {harness: opencode, effort: medium}\n"
        ),
    )

    captured_requests = stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.5",
        model_token="gpt55",
        harness=HarnessId.OPENCODE,
        harness_model="openai/gpt-5.5",
        execution_policy=ResolvedExecutionPolicy(effort="medium"),
        provenance={"harness_source": "project", "effort_source": "project"},
    )

    preview = build_launch_context(
        spawn_id="dry-run-fallback-opencode-medium",
        request=build_primary_spawn_request(request=LaunchRequest(agent="dev-orchestrator")),
        runtime=build_primary_launch_runtime(project_root=tmp_path),
        harness_registry=_registry_with_harnesses(HarnessId.CODEX, HarnessId.OPENCODE),
        dry_run=True,
    )

    assert len(captured_requests) == 1
    assert captured_requests[0].agent == "dev-orchestrator"
    assert captured_requests[0].model_override is None
    assert captured_requests[0].harness_override is None
    assert preview.harness.id is HarnessId.OPENCODE
    assert preview.resolved_request.model == "gpt-5.5"
    assert preview.resolved_request.execution_policy.effort == "medium"
    assert str(preview.binding.run_params.model) == "openai/gpt-5.5"
    assert preview.binding.spec.model == "openai/gpt-5.5"


def test_spawn_prepare_cursor_uses_bundle_harness_model_verbatim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)

    def _which(_command: str) -> str:
        return "/usr/bin/cursor"

    monkeypatch.setattr(cursor_harness.shutil, "which", _which)
    captured_requests = stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-opus-4-7",
        model_token="opus47",
        harness=HarnessId.CURSOR,
        harness_model="claude-opus-4-7-thinking-high",
        execution_policy=ResolvedExecutionPolicy(effort="high"),
        provenance={"harness_source": "cli", "effort_source": "cli"},
    )

    request = build_primary_spawn_request(
        request=LaunchRequest(
            model="opus47",
            harness=HarnessId.CURSOR.value,
            execution_policy=ResolvedExecutionPolicy(effort="high"),
        )
    )
    runtime = build_primary_launch_runtime(project_root=tmp_path).model_copy(
        update={"composition_surface": LaunchCompositionSurface.SPAWN_PREPARE}
    )
    preview = build_launch_context(
        spawn_id="dry-run-cursor-bundle-harness-model",
        request=request,
        runtime=runtime,
        harness_registry=_registry_with_harnesses(HarnessId.CURSOR),
        dry_run=True,
    )

    assert len(captured_requests) == 1
    assert captured_requests[0].model_override == "opus47"
    assert captured_requests[0].harness_override == HarnessId.CURSOR.value
    assert captured_requests[0].effort_override == "high"
    assert preview.harness.id is HarnessId.CURSOR
    assert preview.resolved_request.model == "claude-opus-4-7"
    assert preview.resolved_request.execution_policy.effort == "high"
    assert str(preview.binding.run_params.model) == "claude-opus-4-7-thinking-high"
    assert preview.binding.spec.model == "claude-opus-4-7-thinking-high"
    assert "--model" in preview.binding.argv
    assert (
        preview.binding.argv[preview.binding.argv.index("--model") + 1]
        == "claude-opus-4-7-thinking-high"
    )
    assert preview.model_selection is not None
    assert preview.model_selection.harness_model_id == "claude-opus-4-7-thinking-high"
