# qa-validated: test-suite-redesign
"""Skill variant resolution tests: alias → canonical → harness fallback chain."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.plan import (
    build_primary_launch_runtime,
    build_primary_spawn_request,
)
from meridian.lib.launch.types import LaunchRequest
from tests.support.fixtures import write_agent, write_skill

pytestmark = pytest.mark.slow


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )


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
