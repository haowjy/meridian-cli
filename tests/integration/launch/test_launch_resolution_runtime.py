# qa-validated: test-suite-redesign
"""Runtime building, spawn request construction, and terminal surface mode tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import (
    HarnessRegistry,
    get_default_harness_registry,
)
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.launch_types import ResolvedExecutionPolicy, TerminalSurfaceMode
from meridian.lib.launch.plan import (
    build_primary_launch_runtime,
    build_primary_spawn_request,
)
from meridian.lib.launch.types import LaunchRequest, build_primary_prompt
from meridian.lib.ops.spawn import context_ref
from tests.support.fixtures import write_agent, write_skill
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
        (HarnessId.OPENCODE, False),
        (HarnessId.CODEX, True),
    ],
    ids=["claude", "opencode", "codex"],
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
    else:
        assert spawn_request.prompt == ""


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
    [
        "claude-sonnet-4",
        "gpt-5.4",
        "gemini-2.5-pro",
        "",
    ],
    ids=["claude-model", "codex-model", "opencode-model", "no-model"],
)
def test_build_primary_spawn_request_without_harness_skips_synthetic_prompt(model: str) -> None:
    """Without explicit harness we default to no synthetic first prompt."""

    request = LaunchRequest(model=model)

    spawn_request = build_primary_spawn_request(request=request)

    assert spawn_request.prompt == ""


@pytest.mark.parametrize(
    (
        "model",
        "expected_harness",
        "peer_name",
        "peer_model",
        "skill_name",
        "skill_description",
    ),
    [
        (
            "claude-sonnet-4",
            HarnessId.CLAUDE,
            "coder",
            "gpt-5.4",
            "review",
            "Review helper",
        ),
        (
            "gpt-5.4",
            HarnessId.CODEX,
            "reviewer",
            "claude-sonnet-4",
            "meridian-spawn",
            "Spawn helper",
        ),
        (
            "gemini-2.5-pro",
            HarnessId.OPENCODE,
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
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    expected_harness: HarnessId,
    peer_name: str,
    peer_model: str,
    skill_name: str,
    skill_description: str,
) -> None:
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model=model,
        harness=expected_harness,
    )
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
        else preview.binding.run_params.prompt
    )
    assert "# Meridian Agents" in text
    assert "## Subagent" in text
    assert "- dev-orchestrator" in text
    assert f"- {peer_name}" in text
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
    monkeypatch.setattr(context_ref, "resolve_context_ref", lambda _root, _ref: object())
    monkeypatch.setattr(context_ref, "resolved_context_ref_value", lambda _ref: "p999")
    monkeypatch.setattr(
        context_ref,
        "render_context_refs",
        lambda _refs: '<prior-spawn-context spawn="p999">prior context</prior-spawn-context>',
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
