# qa-validated: test-suite-redesign
"""Skill variant resolution tests: alias → canonical → harness fallback chain."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.bundle_adapter import LoadedSkillEntry
from meridian.lib.launch.composition import AvailableSkillEntry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.launch_types import ResolvedExecutionPolicy
from meridian.lib.launch.plan import (
    build_primary_launch_runtime,
    build_primary_spawn_request,
)
from meridian.lib.launch.types import LaunchRequest
from tests.support.fixtures import write_agent

pytestmark = pytest.mark.slow


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )
    # Prepare-mechanics tests exercise Claude headless spawn-prepare; opt out of the
    # built-in deny_headless_harnesses=["claude"] default. Deny tests overwrite this.
    (project_root / "meridian.toml").write_text(
        "[spawn]\ndeny_headless_harnesses = []\n",
        encoding="utf-8",
    )


@dataclass(frozen=True)
class _BundleResolution:
    model: str
    model_token: str
    harness: HarnessId


@dataclass(frozen=True)
class _FakeBundleResult:
    model: str
    model_token: str
    harness: HarnessId
    harness_model: str | None
    execution_policy: ResolvedExecutionPolicy
    provenance: dict[str, str]
    warnings: tuple[str, ...] = ()
    prompt_surface_inventory_prompt: str = ""
    tools_allowed: tuple[str, ...] = ()
    tools_disallowed: tuple[str, ...] = ()
    tools_mcp: tuple[str, ...] = ()
    skills_loaded: tuple[LoadedSkillEntry, ...] = ()
    skills_available: tuple[AvailableSkillEntry, ...] = ()
    skills_missing: tuple[str, ...] = ()


def _stub_bundle_resolution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resolution: _BundleResolution,
    skills_loaded: tuple[LoadedSkillEntry, ...] = (),
) -> None:
    def _fake_request_and_resolve(
        request: bundle_adapter.BundleRequest,
        *,
        harness_registry: object,
    ) -> _FakeBundleResult:
        _ = (request, harness_registry)
        return _FakeBundleResult(
            model=resolution.model,
            model_token=resolution.model_token,
            harness=resolution.harness,
            harness_model=None,
            execution_policy=ResolvedExecutionPolicy(),
            provenance={"model_source": "cli", "harness_source": "cli"},
            skills_loaded=skills_loaded,
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", _fake_request_and_resolve)


def test_launch_skill_content_flows_from_bundle_to_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bundle-provided skill content flows through policy → composition.

    Variant resolution (alias → canonical → harness) is mars's responsibility.
    Meridian-cli consumes whatever mars resolved and sent in skills.loaded[].
    """
    _write_minimal_mars_config(tmp_path)
    _stub_bundle_resolution(
        monkeypatch,
        resolution=_BundleResolution(
            model="canonical-id",
            model_token="alias-token",
            harness=HarnessId.CODEX,
        ),
        skills_loaded=(
            LoadedSkillEntry(
                name="variant-skill",
                skill_type="reference",
                body="Resolved variant body from mars",
            ),
        ),
    )
    write_agent(
        tmp_path,
        name="dev-orchestrator",
        model="alias-token",
        skills=["variant-skill"],
    )

    preview = build_launch_context(
        spawn_id="dry-run-variant-alias",
        request=build_primary_spawn_request(
            request=LaunchRequest(model="alias-token", agent="dev-orchestrator")
        ),
        runtime=build_primary_launch_runtime(project_root=tmp_path),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    # Codex declares supports_native_skills=True, so skill content is suppressed
    # from the system prompt (it goes via native skill injection).
    system_prompt = preview.projected_content.system_prompt if preview.projected_content else ""
    assert "Resolved variant body from mars" not in system_prompt
    # Skill path is synthetic (from bundle, not disk)
    assert preview.resolved_request.skill_paths
    assert ".mars/skills/variant-skill/SKILL.md" in preview.resolved_request.skill_paths[0]


def test_bundle_skill_content_renders_via_append_for_native_skill_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native-skill harness (Claude) delivers bundle skill body via append-system-prompt."""
    from meridian.lib.launch.request import (
        LaunchArgvIntent,
        LaunchCompositionSurface,
        LaunchRuntime,
        SpawnRequest,
    )

    _write_minimal_mars_config(tmp_path)
    _stub_bundle_resolution(
        monkeypatch,
        resolution=_BundleResolution(
            model="claude-sonnet-4-5",
            model_token="sonnet",
            harness=HarnessId.CLAUDE,
        ),
        skills_loaded=(
            LoadedSkillEntry(
                name="my-principle",
                skill_type="principle",
                body="Always prefer simplicity over cleverness.",
            ),
        ),
    )
    write_agent(
        tmp_path,
        name="dev-orchestrator",
        model="sonnet",
        skills=["my-principle"],
    )

    preview = build_launch_context(
        spawn_id="dry-run-claude-skill-spawn",
        request=SpawnRequest(
            prompt="do the task",
            prompt_is_composed=False,
            model="sonnet",
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

    # Claude delivers skills via --append-system-prompt, not in main system_prompt
    system_prompt = preview.projected_content.system_prompt if preview.projected_content else ""
    assert "Always prefer simplicity over cleverness." not in system_prompt
    assert preview.binding.run_params.appended_system_prompt is not None
    appended = preview.binding.run_params.appended_system_prompt
    assert "Always prefer simplicity over cleverness." in appended
    assert preview.resolved_request.skill_paths
    assert ".mars/skills/my-principle/SKILL.md" in preview.resolved_request.skill_paths[0]
