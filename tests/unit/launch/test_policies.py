from pathlib import Path

import pytest

from meridian.lib.catalog.agent import load_agent_profile
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.config.settings import MeridianConfig
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.harness.registry import HarnessRegistry, get_default_harness_registry
from meridian.lib.launch.policies import (
    SurfacePolicyInput,
    _policy_warnings,
    match_model_policy,
    resolve_launch_policy,
    resolve_policy_fields,
    validate_harness_compatibility,
)
from meridian.lib.launch.request import LaunchCompositionSurface


def _write_agent_profile(project_root: Path, *, name: str, frontmatter: str) -> None:
    path = project_root / ".mars" / "agents" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n\n# {name}\n", encoding="utf-8")


def _mock_alias(
    *,
    alias: str,
    model_id: str,
    harness: HarnessId = HarnessId.CODEX,
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


def test_resolve_policy_fields_resolves_per_field_precedence() -> None:
    resolved = resolve_policy_fields(
        RuntimeOverrides(timeout=12.5),
        RuntimeOverrides(sandbox="workspace-write"),
        RuntimeOverrides(effort="medium", sandbox="read-only"),
        RuntimeOverrides(approval="confirm", autocompact=70000),
        RuntimeOverrides(effort="low", approval="auto", autocompact=30000),
    )

    assert resolved.timeout == 12.5
    assert resolved.sandbox == "workspace-write"
    assert resolved.effort == "medium"
    assert resolved.approval == "confirm"
    assert resolved.autocompact == 70000


def test_resolve_policy_fields_tuple_form_matches_positional_form() -> None:
    tiers = (
        RuntimeOverrides(),
        RuntimeOverrides(effort="high"),
        RuntimeOverrides(effort="medium", approval="auto"),
        RuntimeOverrides(approval="confirm"),
    )

    assert resolve_policy_fields(tiers) == resolve_policy_fields(*tiers)


def test_resolve_policy_fields_model_policy_scope_strips_routing_fields() -> None:
    resolved = resolve_policy_fields(
        RuntimeOverrides(
            model="gpt55",
            harness="codex",
            agent="reviewer",
            effort="high",
        ).model_policy_scope(),
        RuntimeOverrides(
            model="claude",
            harness="claude",
            agent="fallback",
            approval="auto",
        ).model_policy_scope(),
    )

    assert resolved.model is None
    assert resolved.harness is None
    assert resolved.agent is None
    assert resolved.effort == "high"
    assert resolved.approval == "auto"


def test_surface_policy_input_splits_routing_and_execution_layers() -> None:
    registry = get_default_harness_registry()
    surface = SurfacePolicyInput(
        surface=LaunchCompositionSurface.SPAWN_PREPARE,
        catalog=CatalogSession(Path.cwd()),
        layers=(
            RuntimeOverrides(model="gptmini", effort="high", approval="auto"),
            RuntimeOverrides(harness="codex", sandbox="workspace-write"),
        ),
        config_overrides=RuntimeOverrides(agent="reviewer", timeout=20.0),
        config=MeridianConfig(),
        harness_registry=registry,
    )

    assert surface.routing_layers == (
        RuntimeOverrides(model="gptmini"),
        RuntimeOverrides(harness="codex"),
        RuntimeOverrides(agent="reviewer"),
    )
    assert surface.execution_policy_layers == (
        RuntimeOverrides(effort="high", approval="auto"),
        RuntimeOverrides(sandbox="workspace-write"),
        RuntimeOverrides(timeout=20.0),
    )


def test_chat_and_spawn_prepare_surfaces_resolve_equivalent_shared_policy_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alias = _mock_alias(alias="codex", model_id="gpt-5.3-codex", harness=HarnessId.CODEX)
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={"codex": alias, "gpt-5.3-codex": alias},
    )
    registry = get_default_harness_registry()
    layers = (
        RuntimeOverrides(model="codex", approval="auto", sandbox="workspace-write", effort="high"),
        RuntimeOverrides(),
    )
    config = MeridianConfig()

    chat_policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.CHAT,
            catalog=CatalogSession(Path.cwd()),
            layers=layers,
            config_overrides=RuntimeOverrides.from_config(config),
            config=config,
            harness_registry=registry,
            supported_execution_policy_fields=frozenset(
                {"effort", "sandbox", "approval", "autocompact"}
            ),
        )
    )
    spawn_policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(Path.cwd()),
            layers=layers,
            config_overrides=RuntimeOverrides.from_config(config),
            config=config,
            harness_registry=registry,
        )
    )

    assert chat_policy.model == spawn_policy.model == "gpt-5.3-codex"
    assert chat_policy.harness == spawn_policy.harness == HarnessId.CODEX
    assert chat_policy.execution_policy.effort == spawn_policy.execution_policy.effort == "high"
    assert chat_policy.execution_policy.sandbox == spawn_policy.execution_policy.sandbox
    assert chat_policy.execution_policy.approval == spawn_policy.execution_policy.approval


def test_resolve_policy_fields_rejects_mixed_tuple_usage() -> None:
    with pytest.raises(TypeError, match="tier tuple only as its sole argument"):
        resolve_policy_fields(
            RuntimeOverrides(effort="high"),
            (RuntimeOverrides(approval="auto"),),
        )


def test_policy_warnings_preserve_combined_text_format() -> None:
    warnings = _policy_warnings(
        profile_warning="profile missing",
        model_warning="model ambiguous",
    )

    assert [warning.code for warning in warnings] == ["policy_warning"]
    assert warnings[0].message == "profile missing\nmodel ambiguous"


def test_match_model_policy_ranks_model_over_alias_over_glob(tmp_path: Path) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model-policies:\n"
            "  - match: {model-glob: 'gpt-*'}\n"
            "    override: {effort: low}\n"
            "  - match: {alias: fast}\n"
            "    override: {effort: medium}\n"
            "  - match: {model: gpt-5.5}\n"
            "    override: {effort: high}\n"
        ),
    )
    profile = load_agent_profile("reviewer", tmp_path)

    winner = match_model_policy(
        model_policies=profile.model_policies,
        canonical_model_id="gpt-5.5",
        selected_model_token="fast",
    )

    assert winner is not None
    assert winner.match_type == "model"
    assert winner.match_value == "gpt-5.5"


def test_match_model_policy_raises_on_same_rank_ambiguity(tmp_path: Path) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model-policies:\n"
            "  - match: {model-glob: 'gpt-*'}\n"
            "    override: {effort: low}\n"
            "  - match: {model-glob: '*5.5'}\n"
            "    override: {effort: medium}\n"
        ),
    )
    profile = load_agent_profile("reviewer", tmp_path)

    with pytest.raises(ValueError, match="Ambiguous model-policies"):
        match_model_policy(
            model_policies=profile.model_policies,
            canonical_model_id="gpt-5.5",
            selected_model_token="fast",
        )


def test_validate_harness_compatibility_allows_policy_reroute() -> None:
    registry = get_default_harness_registry()
    model_entry = _mock_alias(
        alias="claude",
        model_id="claude-haiku-4-5",
        harness=HarnessId.CLAUDE,
    )

    validate_harness_compatibility(
        model="claude-haiku-4-5",
        harness_id=HarnessId.CODEX,
        model_entry=model_entry,
        harness_registry=registry,
        is_policy_reroute=True,
    )


def test_validate_harness_compatibility_rejects_same_layer_contradiction() -> None:
    registry = get_default_harness_registry()
    model_entry = _mock_alias(
        alias="claude",
        model_id="claude-haiku-4-5",
        harness=HarnessId.CLAUDE,
    )

    with pytest.raises(ValueError, match="incompatible with model"):
        validate_harness_compatibility(
            model="claude-haiku-4-5",
            harness_id=HarnessId.CODEX,
            model_entry=model_entry,
            harness_registry=registry,
            is_policy_reroute=False,
        )


def test_resolve_launch_policy_fallback_prefers_policy_fallback_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: claude\n"
            "model-policies:\n"
            "  - match: {alias: codex}\n"
            "    fallback-order: 2\n"
            "    override: {effort: medium}\n"
            "  - match: {alias: opencode}\n"
            "    fallback-order: 1\n"
            "    override: {effort: low}\n"
        ),
    )
    claude = _mock_alias(alias="claude", model_id="claude-haiku-4-5", harness=HarnessId.CLAUDE)
    codex = _mock_alias(alias="codex", model_id="gpt-5.3-codex", harness=HarnessId.CODEX)
    opencode = _mock_alias(alias="opencode", model_id="kimi-k2.6", harness=HarnessId.OPENCODE)
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={
            "claude": claude,
            "claude-haiku-4-5": claude,
            "codex": codex,
            "gpt-5.3-codex": codex,
            "opencode": opencode,
            "kimi-k2.6": opencode,
        },
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=_registry_with_harnesses(HarnessId.CODEX, HarnessId.OPENCODE),
        )
    )

    assert policy.model == "kimi-k2.6"
    assert policy.harness == HarnessId.OPENCODE


def test_resolve_launch_policy_explicit_model_suppresses_availability_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model-policies:\n"
            "  - match: {alias: codex}\n"
            "    fallback-order: 1\n"
            "    override: {effort: medium}\n"
        ),
    )
    claude = _mock_alias(alias="claude", model_id="claude-haiku-4-5", harness=HarnessId.CLAUDE)
    codex = _mock_alias(alias="codex", model_id="gpt-5.3-codex", harness=HarnessId.CODEX)
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={
            "claude": claude,
            "claude-haiku-4-5": claude,
            "codex": codex,
            "gpt-5.3-codex": codex,
        },
    )

    with pytest.raises(ValueError, match="Unknown or unsupported harness 'claude'"):
        resolve_launch_policy(
            SurfacePolicyInput(
                surface=LaunchCompositionSurface.SPAWN_PREPARE,
                catalog=CatalogSession(tmp_path),
                layers=(
                    RuntimeOverrides(agent="reviewer", model="claude"),
                    RuntimeOverrides(),
                ),
                config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
                config=MeridianConfig(),
                harness_registry=_registry_with_harnesses(HarnessId.CODEX),
            )
        )


def test_resolve_launch_policy_fallback_skips_unresolved_or_unavailable_candidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: claude\n"
            "model-policies:\n"
            "  - match: {alias: missing}\n"
            "    fallback-order: 1\n"
            "    override: {effort: low}\n"
            "  - match: {alias: claude}\n"
            "    fallback-order: 2\n"
            "    override: {effort: medium}\n"
            "  - match: {alias: codex}\n"
            "    fallback-order: 3\n"
            "    override: {effort: high}\n"
        ),
    )
    claude = _mock_alias(alias="claude", model_id="claude-haiku-4-5", harness=HarnessId.CLAUDE)
    codex = _mock_alias(alias="codex", model_id="gpt-5.3-codex", harness=HarnessId.CODEX)
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={
            "claude": claude,
            "claude-haiku-4-5": claude,
            "codex": codex,
            "gpt-5.3-codex": codex,
        },
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=_registry_with_harnesses(HarnessId.CODEX),
        )
    )

    assert policy.model == "gpt-5.3-codex"
    assert policy.harness == HarnessId.CODEX


def test_resolve_launch_policy_rejects_legacy_fanout_profiles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: claude\n"
            "model-policies:\n"
            "  - match: {alias: codex}\n"
            "    fallback-order: 1\n"
            "    override: {effort: medium}\n"
            "fanout:\n"
            "  - alias: opencode\n"
        ),
    )
    claude = _mock_alias(alias="claude", model_id="claude-haiku-4-5", harness=HarnessId.CLAUDE)
    codex = _mock_alias(alias="codex", model_id="gpt-5.3-codex", harness=HarnessId.CODEX)
    opencode = _mock_alias(alias="opencode", model_id="kimi-k2.6", harness=HarnessId.OPENCODE)
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={
            "claude": claude,
            "claude-haiku-4-5": claude,
            "codex": codex,
            "gpt-5.3-codex": codex,
            "opencode": opencode,
            "kimi-k2.6": opencode,
        },
    )

    with pytest.raises(ValueError, match="contains 'fanout' which is no longer supported"):
        resolve_launch_policy(
            SurfacePolicyInput(
                surface=LaunchCompositionSurface.SPAWN_PREPARE,
                catalog=CatalogSession(tmp_path),
                layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
                config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
                config=MeridianConfig(),
                harness_registry=_registry_with_harnesses(HarnessId.CODEX, HarnessId.OPENCODE),
            )
        )


def test_resolve_launch_policy_fallback_preserves_policy_harness_override_for_gpt55(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: claude\n"
            "model-policies:\n"
            "  - match: {alias: gpt55}\n"
            "    fallback-order: 1\n"
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

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=_registry_with_harnesses(HarnessId.CODEX, HarnessId.OPENCODE),
        )
    )

    assert policy.model == "gpt-5.5"
    assert policy.harness == HarnessId.OPENCODE


def test_resolve_launch_policy_fallback_preserves_policy_effort_override_for_gpt55(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: claude\n"
            "model-policies:\n"
            "  - match: {alias: gpt55}\n"
            "    fallback-order: 1\n"
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

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=_registry_with_harnesses(HarnessId.CODEX, HarnessId.OPENCODE),
        )
    )

    assert policy.execution_policy.effort == "medium"


def test_resolve_launch_policy_fallback_does_not_recursively_reapply_model_policies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: claude\n"
            "model-policies:\n"
            "  - match: {alias: opencode}\n"
            "    fallback-order: 1\n"
            "    override: {harness: opencode, effort: medium}\n"
            "  - match: {model: kimi-k2.6}\n"
            "    override: {harness: codex, effort: low}\n"
        ),
    )
    claude = _mock_alias(alias="claude", model_id="claude-haiku-4-5", harness=HarnessId.CLAUDE)
    opencode = _mock_alias(alias="opencode", model_id="kimi-k2.6", harness=HarnessId.OPENCODE)
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={
            "claude": claude,
            "claude-haiku-4-5": claude,
            "opencode": opencode,
            "kimi-k2.6": opencode,
        },
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=_registry_with_harnesses(HarnessId.CODEX, HarnessId.OPENCODE),
        )
    )

    assert policy.model == "kimi-k2.6"
    assert policy.harness == HarnessId.OPENCODE
    assert policy.execution_policy.effort == "medium"


def test_resolve_launch_policy_demoted_candidate_remains_policy_stripped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt55\n"
            "model-policies:\n"
            "  - match: {alias: gpt55}\n"
            "    override: {harness: claude, effort: medium}\n"
        ),
    )
    gpt55 = _mock_alias(
        alias="gpt55",
        model_id="gpt-5.5",
        harness=HarnessId.CODEX,
        default_effort="low",
    )
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={
            "gpt55": gpt55,
            "gpt-5.5": gpt55,
        },
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=_registry_with_harnesses(HarnessId.CODEX),
        )
    )

    assert policy.model == "gpt-5.5"
    assert policy.harness == HarnessId.CODEX
    assert policy.execution_policy.effort == "low"
