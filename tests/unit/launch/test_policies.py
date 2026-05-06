import logging
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.config.settings import MeridianConfig
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.harness.codex import CodexAdapter
from meridian.lib.harness.registry import HarnessRegistry, get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.plan import (
    build_primary_launch_runtime,
    build_primary_spawn_request,
)
from meridian.lib.launch.policies import (
    _policy_warnings,
    _resolve_model_policy_overrides,
    match_model_policy,
    resolve_policy_fields,
    validate_harness_compatibility,
)
from meridian.lib.launch.policies import (
    resolve_policies as _resolve_policies_impl,
)
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SpawnRequest,
)
from meridian.lib.launch.types import LaunchRequest
from meridian.lib.ops.runtime import build_runtime_from_root_and_config
from meridian.lib.ops.spawn.api import spawn_create_sync
from meridian.lib.ops.spawn.models import SpawnCreateInput
from meridian.lib.ops.spawn.prepare import build_create_payload, validate_create_input
from tests.support.fixtures import write_agent


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        "[settings]\n"
        'targets = [".claude"]\n',
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
    harness: HarnessId = HarnessId.CODEX,
    default_effort: str | None = None,
    default_autocompact: int | None = None,
) -> AliasEntry:
    return AliasEntry(
        alias=alias,
        model_id=ModelId(model_id),
        resolved_harness=harness,
        default_effort=default_effort,
        default_autocompact=default_autocompact,
    )


def _patch_alias_resolution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resolved_entries: dict[str, AliasEntry],
    catalog_entries: list[AliasEntry] | None = None,
) -> None:
    def resolve_entry(self: CatalogSession, name: str) -> AliasEntry:
        _ = self
        return resolved_entries.get(
            name,
            _mock_alias(alias="", model_id=name),
        )

    def list_entries(self: CatalogSession) -> list[AliasEntry]:
        _ = self
        return catalog_entries if catalog_entries is not None else list(resolved_entries.values())

    monkeypatch.setattr(CatalogSession, "resolve_model", resolve_entry)
    monkeypatch.setattr(CatalogSession, "load_aliases", list_entries)


def resolve_policies(*, project_root: Path, **kwargs: Any):
    return _resolve_policies_impl(
        catalog=CatalogSession(project_root),
        **kwargs,
    )


def test_resolve_model_policy_overrides_resolves_full_runtime_policy_fields() -> None:
    resolved = resolve_policy_fields(
        RuntimeOverrides(
            model="user-model",
            sandbox="workspace-write",
        ).model_policy_scope(),
        RuntimeOverrides(
            harness="codex",
            approval="auto",
        ).model_policy_scope(),
        RuntimeOverrides(effort="medium", sandbox="read-only"),
        RuntimeOverrides(
            agent="config-agent",
            effort="high",
            approval="confirm",
            autocompact=70,
        ).model_policy_scope(),
        RuntimeOverrides(effort="low", autocompact=30),
    )

    assert resolved.model is None
    assert resolved.harness is None
    assert resolved.agent is None
    assert resolved.sandbox == "workspace-write"
    assert resolved.approval == "auto"
    assert resolved.effort == "medium"
    assert resolved.autocompact == 70


def test_resolve_model_policy_overrides_legacy_wrapper_preserves_named_signature() -> None:
    resolved = _resolve_model_policy_overrides(
        explicit_user_overrides=RuntimeOverrides(),
        profile_model_overrides=RuntimeOverrides(effort="high"),
        profile_defaults=RuntimeOverrides(effort="medium"),
        config_overrides=RuntimeOverrides(effort="low"),
        alias_defaults=RuntimeOverrides(effort="xhigh"),
    )

    assert resolved.effort == "high"


def test_resolve_policy_fields_accepts_tier_tuple() -> None:
    resolved = resolve_policy_fields(
        (
            RuntimeOverrides(),
            RuntimeOverrides(approval="confirm"),
            RuntimeOverrides(approval="auto"),
        )
    )

    assert resolved.approval == "confirm"


def test_resolve_policy_fields_three_tiers_resolve_per_field_precedence() -> None:
    resolved = resolve_policy_fields(
        RuntimeOverrides(effort="high"),
        RuntimeOverrides(effort="medium", approval="auto"),
        RuntimeOverrides(approval="confirm", autocompact=30),
    )

    assert resolved.effort == "high"
    assert resolved.approval == "auto"
    assert resolved.autocompact == 30


def test_resolve_policy_fields_five_tiers_resolve_per_field_precedence() -> None:
    resolved = resolve_policy_fields(
        RuntimeOverrides(),
        RuntimeOverrides(sandbox="workspace-write"),
        RuntimeOverrides(effort="medium", sandbox="read-only"),
        RuntimeOverrides(approval="confirm", autocompact=70),
        RuntimeOverrides(effort="low", approval="auto", autocompact=30),
    )

    assert resolved.sandbox == "workspace-write"
    assert resolved.effort == "medium"
    assert resolved.approval == "confirm"
    assert resolved.autocompact == 70


def test_resolve_policy_fields_seven_tiers_resolve_per_field_precedence() -> None:
    resolved = resolve_policy_fields(
        RuntimeOverrides(),
        RuntimeOverrides(timeout=12.5),
        RuntimeOverrides(),
        RuntimeOverrides(autocompact=80),
        RuntimeOverrides(timeout=20.0, approval="auto"),
        RuntimeOverrides(effort="low", autocompact=30),
        RuntimeOverrides(approval="confirm", effort="medium"),
    )

    assert resolved.timeout == 12.5
    assert resolved.autocompact == 80
    assert resolved.approval == "auto"
    assert resolved.effort == "low"


def test_resolve_policy_fields_empty_tiers_passthrough_to_lower_values() -> None:
    resolved = resolve_policy_fields(
        RuntimeOverrides(),
        RuntimeOverrides(),
        RuntimeOverrides(effort="medium", autocompact=40),
    )

    assert resolved.effort == "medium"
    assert resolved.autocompact == 40


def test_resolve_policy_fields_all_none_resolution_keeps_fields_none() -> None:
    resolved = resolve_policy_fields(
        RuntimeOverrides(),
        RuntimeOverrides(),
        RuntimeOverrides(),
    )

    assert resolved.model is None
    assert resolved.harness is None
    assert resolved.agent is None
    assert resolved.effort is None
    assert resolved.sandbox is None
    assert resolved.approval is None
    assert resolved.autocompact is None
    assert resolved.timeout is None


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


def test_resolve_policy_fields_tuple_form_matches_positional_form() -> None:
    tiers = (
        RuntimeOverrides(),
        RuntimeOverrides(effort="high"),
        RuntimeOverrides(effort="medium", approval="auto"),
        RuntimeOverrides(approval="confirm"),
    )

    assert resolve_policy_fields(tiers) == resolve_policy_fields(*tiers)


def test_resolve_policy_fields_raises_type_error_on_mixed_tuple_usage() -> None:
    with pytest.raises(TypeError, match="tier tuple only as its sole argument"):
        resolve_policy_fields(
            RuntimeOverrides(effort="high"),
            (RuntimeOverrides(approval="auto"),),
        )


def test_policy_warnings_preserve_legacy_combined_text_format() -> None:
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
    from meridian.lib.catalog.agent import load_agent_profile

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
    from meridian.lib.catalog.agent import load_agent_profile

    profile = load_agent_profile("reviewer", tmp_path)

    with pytest.raises(ValueError, match="Ambiguous model-policies"):
        match_model_policy(
            model_policies=profile.model_policies,
            canonical_model_id="gpt-5.5",
            selected_model_token="fast",
        )


def test_validate_harness_compatibility_allows_policy_reroute() -> None:
    registry = get_default_harness_registry()
    model_entry = AliasEntry(
        alias="claude",
        model_id=ModelId("claude-haiku-4-5"),
        resolved_harness=HarnessId.CLAUDE,
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
    model_entry = AliasEntry(
        alias="claude",
        model_id=ModelId("claude-haiku-4-5"),
        resolved_harness=HarnessId.CLAUDE,
    )

    with pytest.raises(ValueError, match="incompatible with model"):
        validate_harness_compatibility(
            model="claude-haiku-4-5",
            harness_id=HarnessId.CODEX,
            model_entry=model_entry,
            harness_registry=registry,
            is_policy_reroute=False,
        )


def test_resolve_policies_warns_and_uses_no_profile_when_config_agent_is_missing(
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(),
        config_overrides=RuntimeOverrides(agent="missing-config-agent"),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    assert policies.profile is None
    assert [warning.code for warning in policies.warnings] == ["profile_warning"]
    assert "missing-config-agent" in policies.warnings[0].message


def test_primary_launch_context_has_no_profile_when_agent_is_unset(tmp_path: Path) -> None:
    _write_minimal_mars_config(tmp_path)
    registry = get_default_harness_registry()

    preview = build_launch_context(
        spawn_id="dry-run-primary",
        request=build_primary_spawn_request(request=LaunchRequest()),
        runtime=build_primary_launch_runtime(project_root=tmp_path),
        harness_registry=registry,
        dry_run=True,
    )

    assert (preview.resolved_request.agent or "") == ""
    assert (preview.resolved_request.agent_metadata.get("session_agent_path") or "") == ""


def test_build_launch_context_surfaces_warning_channel_without_agent_metadata_sidechannel(
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    registry = get_default_harness_registry()

    preview = build_launch_context(
        spawn_id="dry-run-warning",
        request=SpawnRequest(
            prompt="warn",
            model="gpt-5.4",
            harness="codex",
            warning="normalized model alias",
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

    assert preview.resolved_request.warning == "normalized model alias"
    assert [warning.message for warning in preview.warnings] == ["normalized model alias"]
    assert "warning" not in preview.resolved_request.agent_metadata


def test_spawn_prepare_derives_harness_from_model_before_default_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    alias = _mock_alias(
        alias="claude-sonnet-4",
        model_id="claude-sonnet-4",
        harness=HarnessId.CLAUDE,
    )

    def _resolve_model(
        name: str,
        project_root: Path | None = None,
        cache: object | None = None,
    ) -> AliasEntry:
        _ = (name, project_root, cache)
        return alias

    monkeypatch.setattr(
        "meridian.lib.ops.spawn.prepare.resolve_model",
        _resolve_model,
    )
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={"claude-sonnet-4": alias},
        catalog_entries=[alias],
    )
    config = MeridianConfig(default_model="claude-sonnet-4", default_harness="codex")
    runtime = build_runtime_from_root_and_config(tmp_path, config)

    prepared = build_create_payload(
        SpawnCreateInput(
            prompt="derive harness from model",
            project_root=tmp_path.as_posix(),
            dry_run=True,
        ),
        runtime=runtime,
    )

    assert prepared.model is not None
    assert prepared.model.startswith("claude-sonnet-4")
    assert prepared.harness == "claude"


def test_spawn_validation_preserves_alias_token_for_policy_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    alias = _mock_alias(
        alias="gpt55",
        model_id="gpt-5.5",
        default_effort="low",
    )
    canonical = _mock_alias(
        alias="gpt-5.5",
        model_id="gpt-5.5",
        default_effort="high",
    )

    def prepare_resolve_model(
        name: str,
        project_root: Path | None = None,
        *,
        cache: object | None = None,
    ) -> AliasEntry:
        _ = (project_root, cache)
        return {"gpt55": alias, "gpt-5.5": canonical}[name]

    monkeypatch.setattr(
        "meridian.lib.ops.spawn.prepare.resolve_model",
        prepare_resolve_model,
    )
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={"gpt55": alias, "gpt-5.5": canonical},
        catalog_entries=[alias, canonical],
    )

    payload, warning = validate_create_input(
        SpawnCreateInput(
            prompt="test",
            model="gpt55",
            project_root=tmp_path.as_posix(),
            dry_run=True,
        )
    )

    assert warning is None
    assert payload.model == "gpt55"

    prepared = build_create_payload(payload, runtime=build_runtime_from_root_and_config(
        tmp_path, MeridianConfig()
    ))

    assert prepared.model == "gpt-5.5"
    assert prepared.effort == "low"
    assert prepared.model_selection_requested_token == "gpt55"
    assert prepared.model_selection_canonical_id == "gpt-5.5"
    assert prepared.model_selection_harness_provenance == "mars-provided"
    assert 'model_reasoning_effort="low"' in prepared.cli_command


def test_spawn_create_dry_run_wire_includes_prepared_model_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    alias = AliasEntry(
        alias="gpt55",
        model_id=ModelId("gpt-5.5"),
        resolved_harness=HarnessId.CODEX,
    )
    canonical = AliasEntry(
        alias="",
        model_id=ModelId("gpt-5.5"),
        resolved_harness=HarnessId.CODEX,
    )

    def prepare_resolve_model(
        name: str,
        project_root: Path | None = None,
        *,
        cache: object | None = None,
    ) -> AliasEntry:
        _ = (project_root, cache)
        return {"gpt55": alias, "gpt-5.5": canonical}[name]

    monkeypatch.setattr(
        "meridian.lib.ops.spawn.prepare.resolve_model",
        prepare_resolve_model,
    )
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={"gpt55": alias, "gpt-5.5": canonical},
        catalog_entries=[alias, canonical],
    )

    result = spawn_create_sync(
        SpawnCreateInput(
            prompt="test",
            model="gpt55",
            project_root=tmp_path.as_posix(),
            dry_run=True,
        )
    )

    assert result.to_wire()["model_selection"] == {
        "requested_token": "gpt55",
        "canonical_model_id": "gpt-5.5",
        "harness_provenance": "mars-provided",
    }


def test_primary_and_spawn_alias_effort_defaults_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    alias = _mock_alias(
        alias="gpt55",
        model_id="gpt-5.5",
        default_effort="low",
    )
    canonical = _mock_alias(
        alias="gpt-5.5",
        model_id="gpt-5.5",
        default_effort="high",
    )
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={"gpt55": alias, "gpt-5.5": canonical},
        catalog_entries=[alias, canonical],
    )
    registry = get_default_harness_registry()

    primary = build_launch_context(
        spawn_id="dry-run-primary",
        request=build_primary_spawn_request(request=LaunchRequest(model="gpt55")),
        runtime=build_primary_launch_runtime(project_root=tmp_path),
        harness_registry=registry,
        dry_run=True,
    )
    spawn = build_launch_context(
        spawn_id="dry-run-spawn",
        request=SpawnRequest(prompt="test", model="gpt55"),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=registry,
        dry_run=True,
    )

    assert primary.resolved_request.model == "gpt-5.5"
    assert spawn.resolved_request.model == "gpt-5.5"
    assert primary.resolved_request.effort == "low"
    assert spawn.resolved_request.effort == "low"
    assert primary.model_selection is not None
    assert primary.model_selection.requested_token == "gpt55"
    assert primary.model_selection.canonical_model_id == "gpt-5.5"
    assert spawn.model_selection is not None
    assert spawn.model_selection.requested_token == "gpt55"
    assert spawn.model_selection.canonical_model_id == "gpt-5.5"


def test_resolve_policies_cli_model_override_can_replace_profile_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    write_agent(tmp_path, name="explorer", model="gpt-5.4", harness="codex")
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={
            "claude-haiku-4-5": _mock_alias(
                alias="claude-haiku-4-5",
                model_id="claude-haiku-4-5",
                harness=HarnessId.CLAUDE,
            )
        },
    )

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(
            RuntimeOverrides(agent="explorer", model="claude-haiku-4-5"),
            RuntimeOverrides(),
        ),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    assert str(policies.harness) == "claude"


def test_resolve_policies_errors_on_same_layer_user_harness_model_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={
            "claude-haiku-4-5": _mock_alias(
                alias="claude-haiku-4-5",
                model_id="claude-haiku-4-5",
                harness=HarnessId.CLAUDE,
            )
        },
    )
    with pytest.raises(ValueError, match="incompatible with model"):
        resolve_policies(
            project_root=tmp_path,
            layers=(
                RuntimeOverrides(model="claude-haiku-4-5", harness="codex"),
                RuntimeOverrides(),
            ),
            config_overrides=RuntimeOverrides(),
            config=MeridianConfig(),
            harness_registry=get_default_harness_registry(),
            configured_default_harness="codex",
        )


def test_resolve_policies_errors_on_invalid_same_layer_user_model_with_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)

    def reject_model(self: CatalogSession, name: str) -> AliasEntry:
        _ = self
        raise ValueError(f"Invalid model '{name}'.")

    monkeypatch.setattr(CatalogSession, "resolve_model", reject_model)

    with pytest.raises(ValueError, match="Invalid model 'bad-model'"):
        resolve_policies(
            project_root=tmp_path,
            layers=(
                RuntimeOverrides(model="bad-model", harness="codex"),
                RuntimeOverrides(),
            ),
            config_overrides=RuntimeOverrides(),
            config=MeridianConfig(),
            harness_registry=get_default_harness_registry(),
            configured_default_harness="codex",
        )


def test_resolve_policies_profile_model_overrides_win_over_config_and_alias_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt\n"
            "effort: high\n"
            "autocompact: 80\n"
            "models:\n"
            "  gpt:\n"
            "    effort: medium\n"
            "    autocompact: 35\n"
        ),
    )

    aliases = {
        "gpt": _mock_alias(
            alias="gpt",
            model_id="gpt-5.5",
            default_effort="low",
            default_autocompact=20,
        ),
        "gpt-5.5": _mock_alias(
            alias="gpt",
            model_id="gpt-5.5",
            default_effort="low",
            default_autocompact=20,
        ),
    }
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries=aliases,
        catalog_entries=[aliases["gpt"]],
    )

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(effort="xhigh", autocompact=99),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    assert policies.resolved_overrides.effort == "medium"
    assert policies.resolved_overrides.autocompact == 35


def test_resolve_policies_model_policy_overrides_win_over_legacy_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt\n"
            "models:\n"
            "  gpt:\n"
            "    effort: low\n"
            "model-policies:\n"
            "  - match: {alias: gpt}\n"
            "    override:\n"
            "      effort: high\n"
            "      autocompact: 40\n"
        ),
    )
    aliases = {"gpt": _mock_alias(alias="gpt", model_id="gpt-5.5")}
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries=aliases,
        catalog_entries=[aliases["gpt"]],
    )

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    assert policies.resolved_overrides.effort == "high"
    assert policies.resolved_overrides.autocompact == 40


def test_resolve_policies_model_policy_harness_participates_in_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt\n"
            "harness: opencode\n"
            "model-policies:\n"
            "  - match: {alias: gpt}\n"
            "    override: {harness: claude}\n"
        ),
    )
    aliases = {"gpt": _mock_alias(alias="gpt", model_id="gpt-5.5", harness=HarnessId.CODEX)}
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries=aliases,
        catalog_entries=[aliases["gpt"]],
    )

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    assert policies.harness == HarnessId.CLAUDE
    assert policies.model_selection is not None
    assert policies.model_selection.harness_provenance == "profile-model-policy"


def test_resolve_policies_model_policy_scalar_overrides_flow_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt\n"
            "model-policies:\n"
            "  - match: {alias: gpt}\n"
            "    override:\n"
            "      sandbox: workspace-write\n"
            "      approval: auto\n"
            "      timeout: 12.5\n"
        ),
    )
    aliases = {"gpt": _mock_alias(alias="gpt", model_id="gpt-5.5")}
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries=aliases,
        catalog_entries=[aliases["gpt"]],
    )

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    assert policies.resolved_overrides.sandbox == "workspace-write"
    assert policies.resolved_overrides.approval == "auto"
    assert policies.resolved_overrides.timeout == 12.5


def test_resolve_policies_profile_generic_approval_flows_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt\n"
            "approval: auto\n"
        ),
    )
    aliases = {"gpt": _mock_alias(alias="gpt", model_id="gpt-5.5")}
    _patch_alias_resolution(monkeypatch, resolved_entries=aliases, catalog_entries=[aliases["gpt"]])

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    assert policies.resolved_overrides.approval == "auto"


def test_resolve_policies_profile_generic_sandbox_flows_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt\n"
            "sandbox: workspace-write\n"
        ),
    )
    aliases = {"gpt": _mock_alias(alias="gpt", model_id="gpt-5.5")}
    _patch_alias_resolution(monkeypatch, resolved_entries=aliases, catalog_entries=[aliases["gpt"]])

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    assert policies.resolved_overrides.sandbox == "workspace-write"


def test_resolve_policies_user_approval_beats_profile_generic_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt\n"
            "approval: auto\n"
        ),
    )
    aliases = {"gpt": _mock_alias(alias="gpt", model_id="gpt-5.5")}
    _patch_alias_resolution(monkeypatch, resolved_entries=aliases, catalog_entries=[aliases["gpt"]])

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer", approval="yolo"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    assert policies.resolved_overrides.approval == "yolo"


def test_resolve_policies_model_policy_approval_beats_profile_generic_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt\n"
            "approval: auto\n"
            "model-policies:\n"
            "  - match: {alias: gpt}\n"
            "    override: {approval: confirm}\n"
        ),
    )
    aliases = {"gpt": _mock_alias(alias="gpt", model_id="gpt-5.5")}
    _patch_alias_resolution(monkeypatch, resolved_entries=aliases, catalog_entries=[aliases["gpt"]])

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    assert policies.resolved_overrides.approval == "confirm"


def test_resolve_policies_cli_harness_beats_model_policy_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt\n"
            "model-policies:\n"
            "  - match: {alias: gpt}\n"
            "    override: {harness: claude}\n"
        ),
    )
    aliases = {"gpt": _mock_alias(alias="gpt", model_id="gpt-5.5", harness=HarnessId.CODEX)}
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries=aliases,
        catalog_entries=[aliases["gpt"]],
    )

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer", harness="codex"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="claude",
    )

    assert policies.harness == HarnessId.CODEX
    assert policies.model_selection is not None
    assert policies.model_selection.harness_provenance == "explicit-override"


def test_resolve_policies_falls_back_to_first_available_fanout_before_model_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: claude-choice\n"
            "fanout:\n"
            "  - alias: unavailable-too\n"
            "  - alias: codex-fanout\n"
            "model-policies:\n"
            "  - match: {model: codex-policy}\n"
            "    override: {effort: low}\n"
        ),
    )
    aliases = {
        "claude-choice": _mock_alias(
            alias="claude-choice", model_id="claude-haiku-4-5", harness=HarnessId.CLAUDE
        ),
        "unavailable-too": _mock_alias(
            alias="unavailable-too", model_id="opencode-model", harness=HarnessId.OPENCODE
        ),
        "codex-fanout": _mock_alias(
            alias="codex-fanout", model_id="gpt-5.5", harness=HarnessId.CODEX
        ),
        "codex-policy": _mock_alias(
            alias="", model_id="gpt-5.4", harness=HarnessId.CODEX
        ),
    }
    _patch_alias_resolution(monkeypatch, resolved_entries=aliases)
    registry = HarnessRegistry()
    registry.register(CodexAdapter())

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=registry,
        configured_default_harness="claude",
    )

    assert policies.model == "gpt-5.5"
    assert policies.harness == HarnessId.CODEX
    assert policies.model_selection is not None
    assert policies.model_selection.requested_token == "claude-choice"
    assert policies.model_selection.selected_model_token == "codex-fanout"
    assert policies.model_selection.canonical_model_id == "gpt-5.5"
    assert policies.model_selection.harness_provenance == "availability-fallback"


def test_resolve_policies_fanout_skips_raw_model_with_unresolvable_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: claude-choice\n"
            "fanout:\n"
            "  - alias: opencode-go/kimi-k2.6\n"
            "  - alias: codex-fanout\n"
        ),
    )

    aliases = {
        "claude-choice": _mock_alias(
            alias="claude-choice", model_id="claude-haiku-4-5", harness=HarnessId.CLAUDE
        ),
        "opencode-go/kimi-k2.6": AliasEntry(
            alias="",
            model_id=ModelId("opencode-go/kimi-k2.6"),
            resolved_harness=None,
        ),
        "codex-fanout": _mock_alias(
            alias="codex-fanout", model_id="gpt-5.5", harness=HarnessId.CODEX
        ),
    }
    _patch_alias_resolution(monkeypatch, resolved_entries=aliases)
    registry = HarnessRegistry()
    registry.register(CodexAdapter())

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=registry,
        configured_default_harness="claude",
    )

    assert policies.model == "gpt-5.5"
    assert policies.harness == HarnessId.CODEX
    assert policies.model_selection is not None
    assert policies.model_selection.requested_token == "claude-choice"
    assert policies.model_selection.selected_model_token == "codex-fanout"
    assert policies.model_selection.harness_provenance == "availability-fallback"


def test_resolve_policies_profile_policy_matching_fanout_token_does_not_transform_fanout_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: claude-choice\n"
            "fanout:\n"
            "  - alias: codex-fanout\n"
            "model-policies:\n"
            "  - match: {alias: codex-fanout}\n"
            "    override: {harness: claude, effort: high}\n"
        ),
    )
    aliases = {
        "claude-choice": _mock_alias(
            alias="claude-choice",
            model_id="claude-haiku-4-5",
            harness=HarnessId.CLAUDE,
        ),
        "codex-fanout": _mock_alias(
            alias="codex-fanout",
            model_id="gpt-5.5",
            harness=HarnessId.CODEX,
        ),
    }
    _patch_alias_resolution(monkeypatch, resolved_entries=aliases)
    registry = HarnessRegistry()
    registry.register(CodexAdapter())

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=registry,
        configured_default_harness="claude",
    )

    assert policies.model == "gpt-5.5"
    assert policies.harness == HarnessId.CODEX
    assert policies.resolved_overrides.effort is None
    assert policies.model_selection is not None
    assert policies.model_selection.selected_model_token == "codex-fanout"
    assert policies.model_selection.harness_provenance == "availability-fallback"


def test_resolve_policies_fanout_candidate_chain_avoids_profile_policy_ambiguity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: claude-choice\n"
            "fanout:\n"
            "  - alias: codex-fanout\n"
            "model-policies:\n"
            "  - match: {model-glob: 'gpt-*'}\n"
            "    override: {effort: low}\n"
            "  - match: {model-glob: '*5.5'}\n"
            "    override: {effort: high}\n"
        ),
    )
    aliases = {
        "claude-choice": _mock_alias(
            alias="claude-choice",
            model_id="claude-haiku-4-5",
            harness=HarnessId.CLAUDE,
        ),
        "codex-fanout": _mock_alias(
            alias="codex-fanout",
            model_id="gpt-5.5",
            harness=HarnessId.CODEX,
        ),
    }
    _patch_alias_resolution(monkeypatch, resolved_entries=aliases)
    registry = HarnessRegistry()
    registry.register(CodexAdapter())

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=registry,
        configured_default_harness="claude",
    )

    assert policies.model == "gpt-5.5"
    assert policies.harness == HarnessId.CODEX
    assert policies.resolved_overrides.effort is None
    assert policies.model_selection is not None
    assert policies.model_selection.selected_model_token == "codex-fanout"
    assert policies.model_selection.harness_provenance == "availability-fallback"


def test_resolve_policies_model_policy_promotion_demotes_previous_candidate_for_availability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: codex-choice\n"
            "model-policies:\n"
            "  - match: {alias: codex-choice}\n"
            "    override: {harness: claude, effort: high}\n"
        ),
    )
    aliases = {
        "codex-choice": _mock_alias(
            alias="codex-choice", model_id="gpt-5.5", harness=HarnessId.CODEX
        ),
    }
    _patch_alias_resolution(monkeypatch, resolved_entries=aliases)
    registry = HarnessRegistry()
    registry.register(CodexAdapter())

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=registry,
        configured_default_harness="claude",
    )

    assert policies.model == "gpt-5.5"
    assert policies.harness == HarnessId.CODEX
    assert policies.resolved_overrides.effort is None
    assert policies.model_selection is not None
    assert policies.model_selection.requested_token == "codex-choice"
    assert policies.model_selection.selected_model_token == "codex-choice"
    assert policies.model_selection.canonical_model_id == "gpt-5.5"
    assert policies.model_selection.harness_provenance == "availability-fallback"


def test_resolve_policies_raises_when_candidate_chain_exhausts_available_harnesses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: claude-choice\n"
            "fanout:\n"
            "  - alias: unavailable-fanout\n"
            "model-policies:\n"
            "  - match: {alias: unavailable-alias}\n"
            "    override: {effort: low}\n"
            "  - match: {model: unavailable-model}\n"
            "    override: {autocompact: 44}\n"
            "  - match: {model-glob: 'gpt-*'}\n"
            "    override: {harness: codex, effort: high}\n"
        ),
    )

    aliases = {
        "claude-choice": _mock_alias(
            alias="claude-choice", model_id="claude-haiku-4-5", harness=HarnessId.CLAUDE
        ),
        "unavailable-fanout": _mock_alias(
            alias="unavailable-fanout", model_id="opencode-fanout", harness=HarnessId.OPENCODE
        ),
        "unavailable-alias": _mock_alias(
            alias="unavailable-alias", model_id="opencode-alias", harness=HarnessId.OPENCODE
        ),
        "unavailable-model": _mock_alias(
            alias="", model_id="unavailable-model", harness=HarnessId.OPENCODE
        ),
    }
    _patch_alias_resolution(monkeypatch, resolved_entries=aliases)
    registry = HarnessRegistry()
    registry.register(CodexAdapter())

    with pytest.raises(ValueError, match="Unknown or unsupported harness 'claude'"):
        resolve_policies(
            project_root=tmp_path,
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides(),
            config=MeridianConfig(),
            harness_registry=registry,
            configured_default_harness="claude",
        )


def test_resolve_policies_explicit_model_skips_harness_availability_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "fanout:\n"
            "  - alias: codex-fanout\n"
        ),
    )
    aliases = {
        "claude-choice": _mock_alias(
            alias="claude-choice", model_id="claude-haiku-4-5", harness=HarnessId.CLAUDE
        ),
        "codex-fanout": _mock_alias(
            alias="codex-fanout", model_id="gpt-5.5", harness=HarnessId.CODEX
        ),
    }
    _patch_alias_resolution(monkeypatch, resolved_entries=aliases)
    registry = HarnessRegistry()
    registry.register(CodexAdapter())

    with pytest.raises(ValueError, match="Unknown or unsupported harness 'claude'"):
        resolve_policies(
            project_root=tmp_path,
            layers=(RuntimeOverrides(agent="reviewer", model="claude-choice"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides(),
            config=MeridianConfig(),
            harness_registry=registry,
            configured_default_harness="claude",
        )


def test_resolve_policies_primary_harness_does_not_override_model_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    aliases = {
        "gpt": _mock_alias(alias="gpt", model_id="gpt-5.5", harness=HarnessId.CODEX),
    }
    _patch_alias_resolution(monkeypatch, resolved_entries=aliases)

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(model="gpt"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(harness="claude"),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="claude",
    )

    assert policies.harness == HarnessId.CODEX


def test_resolve_policies_user_effort_override_wins_over_model_profile_and_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt\n"
            "effort: high\n"
            "autocompact: 80\n"
            "models:\n"
            "  gpt:\n"
            "    effort: medium\n"
            "    autocompact: 35\n"
        ),
    )

    aliases = {
        "gpt": _mock_alias(
            alias="gpt",
            model_id="gpt-5.5",
            default_effort="low",
            default_autocompact=20,
        ),
        "gpt-5.5": _mock_alias(
            alias="gpt",
            model_id="gpt-5.5",
            default_effort="low",
            default_autocompact=20,
        ),
    }
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries=aliases,
        catalog_entries=[aliases["gpt"]],
    )

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(
            RuntimeOverrides(agent="reviewer", effort="xhigh", autocompact=90),
            RuntimeOverrides(),
        ),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    assert policies.resolved_overrides.effort == "xhigh"
    assert policies.resolved_overrides.autocompact == 90


def test_resolve_policies_cli_effort_override_beats_config_within_explicit_user_layer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt\n"
            "effort: low\n"
            "autocompact: 80\n"
            "models:\n"
            "  gpt:\n"
            "    effort: high\n"
            "    autocompact: 35\n"
        ),
    )

    aliases = {
        "gpt": _mock_alias(
            alias="gpt",
            model_id="gpt-5.5",
            default_effort="medium",
            default_autocompact=20,
        ),
    }
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries=aliases,
        catalog_entries=[aliases["gpt"]],
    )

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(
            RuntimeOverrides(agent="reviewer", effort="xhigh", autocompact=90),
            RuntimeOverrides(),
        ),
        config_overrides=RuntimeOverrides(effort="medium", autocompact=70),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    assert policies.resolved_overrides.effort == "xhigh"
    assert policies.resolved_overrides.autocompact == 90


def test_resolve_policies_models_fallback_matches_model_id_and_warns_on_multiple(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt\n"
            "models:\n"
            "  gpt55:\n"
            "    effort: medium\n"
            "  gpt-latest:\n"
            "    effort: high\n"
        ),
    )

    aliases = {
        "gpt": _mock_alias(alias="", model_id="gpt-5.5"),
        "gpt-5.5": _mock_alias(alias="", model_id="gpt-5.5"),
    }
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries=aliases,
        catalog_entries=[
            _mock_alias(alias="gpt55", model_id="gpt-5.5"),
            _mock_alias(alias="gpt-latest", model_id="gpt-5.5"),
        ],
    )

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    assert policies.resolved_overrides.effort == "medium"
    assert [warning.code for warning in policies.warnings] == ["model_warning"]
    assert "ignoring: gpt-latest" in policies.warnings[0].message


def test_resolve_policies_exact_alias_match_beats_model_id_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt\n"
            "models:\n"
            "  gpt:\n"
            "    effort: medium\n"
            "  gpt55:\n"
            "    effort: high\n"
        ),
    )

    aliases = {
        "gpt": _mock_alias(alias="gpt", model_id="gpt-5.5"),
        "gpt-5.5": _mock_alias(alias="gpt", model_id="gpt-5.5"),
    }
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries=aliases,
        catalog_entries=[_mock_alias(alias="gpt55", model_id="gpt-5.5")],
    )

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    assert policies.resolved_overrides.effort == "medium"
    assert policies.warnings == ()


def test_resolve_policies_unmatched_models_entry_logs_debug_and_uses_profile_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt\n"
            "effort: high\n"
            "models:\n"
            "  gpt55:\n"
            "    effort: medium\n"
        ),
    )

    aliases = {
        "gpt": _mock_alias(alias="gpt", model_id="gpt-5.5"),
        "gpt-5.5": _mock_alias(alias="gpt", model_id="gpt-5.5"),
    }
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries=aliases,
        catalog_entries=[_mock_alias(alias="gpt55", model_id="gpt-5.5-mini")],
    )

    with caplog.at_level(logging.DEBUG, logger="meridian.lib.launch.policies"):
        policies = resolve_policies(
            project_root=tmp_path,
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides(),
            config=MeridianConfig(),
            harness_registry=get_default_harness_registry(),
            configured_default_harness="codex",
        )

    assert policies.resolved_overrides.effort == "high"
    assert policies.warnings == ()
    assert "generic model-policy defaults but no matching models entry" in caplog.text


def test_resolve_policies_unmatched_model_policy_logs_generic_effort_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt\n"
            "effort: high\n"
            "model-policies:\n"
            "  - match: {alias: sonnet}\n"
            "    override: {effort: medium}\n"
        ),
    )

    aliases = {"gpt": _mock_alias(alias="gpt", model_id="gpt-5.5")}
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries=aliases,
        catalog_entries=[aliases["gpt"]],
    )

    with caplog.at_level(logging.DEBUG, logger="meridian.lib.launch.policies"):
        policies = resolve_policies(
            project_root=tmp_path,
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides(),
            config=MeridianConfig(),
            harness_registry=get_default_harness_registry(),
            configured_default_harness="codex",
        )

    assert policies.resolved_overrides.effort == "high"
    assert (
        "No model-policies rule matched for 'gpt-5.5'; using generic profile "
        "model-policy defaults."
    ) in caplog.text


def test_resolve_policies_cli_alias_does_not_double_resolve_final_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="architect",
        frontmatter=(
            "name: architect\n"
            "model: gpt-5.4\n"
            "effort: high\n"
        ),
    )

    calls: list[str] = []

    def resolve_once(self: CatalogSession, name: str) -> AliasEntry:
        _ = self
        calls.append(name)
        if name == "gpt":
            return _mock_alias(alias="gpt", model_id="gpt-5.4")
        if name == "gpt-5.4":
            return _mock_alias(alias="", model_id="gpt-5.4-mini")
        return _mock_alias(alias="", model_id=name)

    def list_gpt_alias(self: CatalogSession) -> list[AliasEntry]:
        _ = self
        return [_mock_alias(alias="gpt", model_id="gpt-5.4")]

    monkeypatch.setattr(CatalogSession, "resolve_model", resolve_once)
    monkeypatch.setattr(CatalogSession, "load_aliases", list_gpt_alias)

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="architect", model="gpt"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    assert policies.model == "gpt-5.4"
    assert "gpt-5.4" not in calls
    assert policies.model_selection is not None
    assert policies.model_selection.requested_token == "gpt"
    assert policies.model_selection.selected_model_token == "gpt"
    assert policies.model_selection.canonical_model_id == "gpt-5.4"
    assert policies.model_selection.mars_provided_harness == HarnessId.CODEX
    assert policies.model_selection.resolved_entry is not None
    assert policies.model_selection.harness_provenance == "mars-provided"


def test_resolve_policies_model_id_entry_wins_over_generic_profile_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="dev-orchestrator",
        frontmatter=(
            "name: dev-orchestrator\n"
            "harness: claude\n"
            "effort: high\n"
            "models:\n"
            "  gpt-5.5:\n"
            "    effort: medium\n"
        ),
    )

    aliases = {
        "gpt55": _mock_alias(alias="gpt55", model_id="gpt-5.5"),
        "gpt-5.5": _mock_alias(alias="", model_id="gpt-5.5"),
    }
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries=aliases,
        catalog_entries=[aliases["gpt55"]],
    )

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(
            RuntimeOverrides(agent="dev-orchestrator", model="gpt55", harness="codex"),
            RuntimeOverrides(),
        ),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="claude",
    )

    assert policies.model == "gpt-5.5"
    assert policies.harness == HarnessId.CODEX
    assert policies.resolved_overrides.effort == "medium"


def test_resolve_policies_profile_effort_blocks_alias_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Layer 3 (profile default effort) should win over config and alias defaults."""
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt\n"
            "effort: medium\n"
        ),
    )

    aliases = {
        "gpt": _mock_alias(
            alias="gpt",
            model_id="gpt-5.5",
            default_effort="low",
            default_autocompact=20,
        ),
    }
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries=aliases,
        catalog_entries=[aliases["gpt"]],
    )

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(effort="xhigh", autocompact=90),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    # Profile effort (medium) wins over config (xhigh) and alias default (low)
    assert policies.resolved_overrides.effort == "medium"
    # Config autocompact passes through since profile has no autocompact
    assert policies.resolved_overrides.autocompact == 90


def test_resolve_policies_model_overrides_win_over_profile_and_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Layer 2 (agent models[alias] entry) should win over lower layers."""
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt\n"
            "effort: low\n"
            "autocompact: 80\n"
            "models:\n"
            "  gpt:\n"
            "    effort: high\n"
            "    autocompact: 50\n"
        ),
    )

    aliases = {
        "gpt": _mock_alias(
            alias="gpt",
            model_id="gpt-5.5",
            default_effort="medium",
            default_autocompact=25,
        ),
    }
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries=aliases,
        catalog_entries=[aliases["gpt"]],
    )

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    # models[gpt].effort (high) wins over profile effort (low) and alias default (medium)
    assert policies.resolved_overrides.effort == "high"
    # models[gpt].autocompact (50) wins over profile autocompact (80) and alias (25)
    assert policies.resolved_overrides.autocompact == 50


def test_resolve_policies_alias_defaults_passthrough_when_nothing_else_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Layer 5 (alias defaults) should win when no higher layers set effort/autocompact."""
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt\n"
        ),
    )

    aliases = {
        "gpt": _mock_alias(
            alias="gpt",
            model_id="gpt-5.5",
            default_effort="low",
            default_autocompact=30,
        ),
    }
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries=aliases,
        catalog_entries=[aliases["gpt"]],
    )

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    assert policies.resolved_overrides.effort == "low"
    assert policies.resolved_overrides.autocompact == 30


def test_resolve_policies_config_overrides_win_over_alias_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Layer 4 (config) should win over Layer 5 (alias defaults)."""
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt\n"
        ),
    )

    aliases = {
        "gpt": _mock_alias(
            alias="gpt",
            model_id="gpt-5.5",
            default_effort="low",
            default_autocompact=30,
        ),
    }
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries=aliases,
        catalog_entries=[aliases["gpt"]],
    )

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(effort="high", autocompact=70),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    assert policies.resolved_overrides.effort == "high"
    assert policies.resolved_overrides.autocompact == 70


def test_resolve_policies_harness_default_alias_canonicalizes_final_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    alias = _mock_alias(alias="gpt", model_id="gpt-5.4", harness=HarnessId.CODEX)
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={"gpt": alias},
        catalog_entries=[alias],
    )

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig.model_validate(
            {"harness": {"codex": {"model": "gpt"}}}
        ),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    assert policies.model == "gpt-5.4"
    assert policies.model_selection is not None
    assert policies.model_selection.requested_token == "gpt-5.4"
    assert policies.model_selection.selected_model_token == "gpt"
    assert policies.model_selection.canonical_model_id == "gpt-5.4"


def test_resolve_policies_no_overrides_at_any_layer_yields_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no layer sets effort/autocompact, both should be None."""
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt\n"
        ),
    )

    aliases = {
        "gpt": _mock_alias(alias="gpt", model_id="gpt-5.5"),
    }
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries=aliases,
        catalog_entries=[aliases["gpt"]],
    )

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    assert policies.resolved_overrides.effort is None
    assert policies.resolved_overrides.autocompact is None


def test_resolve_policies_fallback_winner_is_not_re_resolved_after_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="architect",
        frontmatter=(
            "name: architect\n"
            "model: claude-choice\n"
            "fanout:\n"
            "  - alias: codex-fanout\n"
        ),
    )

    calls: list[str] = []

    def resolve_once(self: CatalogSession, name: str) -> AliasEntry:
        _ = self
        calls.append(name)
        if name == "claude-choice":
            return _mock_alias(
                alias="claude-choice",
                model_id="claude-haiku-4-5",
                harness=HarnessId.CLAUDE,
            )
        if name == "codex-fanout":
            return _mock_alias(
                alias="codex-fanout",
                model_id="gpt-5.5",
                harness=HarnessId.CODEX,
            )
        return _mock_alias(alias="", model_id=name)

    def _load_aliases_for_fallback_test(self: CatalogSession) -> list[AliasEntry]:
        _ = self
        return [_mock_alias(alias="codex-fanout", model_id="gpt-5.5", harness=HarnessId.CODEX)]

    monkeypatch.setattr(CatalogSession, "resolve_model", resolve_once)
    monkeypatch.setattr(
        CatalogSession,
        "load_aliases",
        _load_aliases_for_fallback_test,
    )
    registry = HarnessRegistry()
    registry.register(CodexAdapter())

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="architect"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=registry,
        configured_default_harness="claude",
    )

    assert policies.model == "gpt-5.5"
    assert calls.count("claude-choice") == 1
    assert calls.count("codex-fanout") == 1
    assert "gpt-5.5" not in calls


def test_resolve_policies_model_selection_preserves_requested_alias_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    alias = _mock_alias(alias="fast", model_id="gpt-5.5", harness=HarnessId.CODEX)
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={"fast": alias},
        catalog_entries=[alias],
    )

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(model="fast"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=MeridianConfig(),
        harness_registry=get_default_harness_registry(),
        configured_default_harness="codex",
    )

    assert policies.model == "gpt-5.5"
    assert policies.model_selection is not None
    assert policies.model_selection.requested_token == "fast"
    assert policies.model_selection.selected_model_token == "fast"
    assert policies.model_selection.canonical_model_id == "gpt-5.5"
