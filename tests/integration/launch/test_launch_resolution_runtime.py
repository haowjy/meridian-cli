# qa-validated: test-suite-redesign
"""Runtime building, spawn request construction, and terminal surface mode tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.harness.registry import (
    HarnessRegistry,
    get_default_harness_registry,
)
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.launch_types import TerminalSurfaceMode
from meridian.lib.launch.plan import (
    build_primary_launch_runtime,
    build_primary_spawn_request,
)
from meridian.lib.launch.types import LaunchRequest, build_primary_prompt
from tests.support.fixtures import write_agent, write_skill

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


def _mock_alias(
    *,
    alias: str,
    model_id: str,
    harness: HarnessId,
    default_effort: str | None = None,
) -> AliasEntry:
    return AliasEntry(
        alias=alias,
        model_id=ModelId(model_id),
        resolved_harness=harness,
        default_effort=default_effort,
    )


def _patch_alias_resolution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resolved_entries: dict[str, AliasEntry],
) -> None:
    def resolve_entry(self: CatalogSession, name: str) -> AliasEntry:
        _ = self
        try:
            return resolved_entries[name]
        except KeyError as exc:
            raise ValueError(f"Unknown model alias '{name}'") from exc

    def list_entries(self: CatalogSession) -> list[AliasEntry]:
        _ = self
        return list(resolved_entries.values())

    monkeypatch.setattr(CatalogSession, "resolve_model", resolve_entry)
    monkeypatch.setattr(CatalogSession, "load_aliases", list_entries)


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


@pytest.mark.parametrize(
    ("model", "requires_initial_prompt"),
    [
        ("claude-sonnet-4", False),
        ("gpt-5.4", True),
        ("gemini-2.5-pro", False),
        ("", False),
    ],
    ids=["claude-model", "codex-model", "opencode-model", "no-model"],
)
def test_build_primary_spawn_request_infers_prompt_requirement_from_model(
    model: str,
    requires_initial_prompt: bool,
) -> None:
    """When harness is not explicit, prompt requirement is inferred from the model."""

    request = LaunchRequest(model=model)

    spawn_request = build_primary_spawn_request(request=request)

    if requires_initial_prompt:
        assert spawn_request.prompt == build_primary_prompt(request)
    else:
        assert spawn_request.prompt == ""


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
        else preview.binding.run_params.prompt
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


def test_launch_resolution_fallback_policy_resolves_opencode_medium(
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

    claude = _mock_alias(alias="claude", model_id="claude-haiku-4-5", harness=HarnessId.CLAUDE)
    gpt55 = _mock_alias(
        alias="gpt55",
        model_id="gpt-5.5",
        harness=HarnessId.CODEX,
        default_effort="low",
    )
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={
            "claude": claude,
            "claude-haiku-4-5": claude,
            "gpt55": gpt55,
            "gpt-5.5": gpt55,
        },
    )

    preview = build_launch_context(
        spawn_id="dry-run-fallback-opencode-medium",
        request=build_primary_spawn_request(request=LaunchRequest(agent="dev-orchestrator")),
        runtime=build_primary_launch_runtime(project_root=tmp_path),
        harness_registry=_registry_with_harnesses(HarnessId.CODEX, HarnessId.OPENCODE),
        dry_run=True,
    )

    assert preview.harness.id is HarnessId.OPENCODE
    assert preview.resolved_request.model == "gpt-5.5"
    assert preview.resolved_request.execution_policy.effort == "medium"
