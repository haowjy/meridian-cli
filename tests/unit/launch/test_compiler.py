from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import pytest

from meridian.lib.catalog.agent import AgentModelEntry, ModelPolicyRule
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.config.settings import AgentOverlayConfig, AgentOverlayModelPolicy, MeridianConfig
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.harness.adapter import SubprocessHarness
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.compiler import (
    CompilerRequest,
    CompilerResult,
    FieldProvenance,
    ProvenanceLevel,
    compile_launch_params,
    compiler_result_to_dry_run_dict,
    render_provenance,
)
from meridian.lib.launch.policies import resolve_policies
from meridian.lib.launch.resolve import ResolvedSkills


def _alias_entry(alias: str = "gptmini") -> AliasEntry:
    return AliasEntry(
        alias=alias,
        model_id=ModelId("openai/gpt-5.4-mini"),
        resolved_harness=HarnessId.CODEX,
        default_effort="medium",
        default_autocompact=50000,
    )


def _request(
    *,
    cli: RuntimeOverrides | None = None,
    env: RuntimeOverrides | None = None,
    overlay: AgentOverlayConfig | None = None,
    config: RuntimeOverrides | None = None,
    profile_routing_model: str | None = None,
    profile_routing_harness: str | None = None,
    profile_policy_defaults: ResolvedExecutionPolicy | None = None,
    profile_model_policies: tuple[ModelPolicyRule, ...] | None = None,
    profile_legacy_models: dict[str, AgentModelEntry] | None = None,
    alias_entry: AliasEntry | None = None,
    alias_catalog: dict[str, AliasEntry] | None = None,
    supported_execution_policy_fields: tuple[str, ...] | frozenset[str] | None = None,
) -> CompilerRequest:
    resolved_alias = alias_entry
    resolved_catalog = alias_catalog
    if resolved_catalog is None:
        resolved_catalog = {alias_entry.alias: alias_entry} if alias_entry is not None else {}
    return CompilerRequest(
        requested_agent="coder",
        cli_overrides=cli or RuntimeOverrides(),
        env_overrides=env or RuntimeOverrides(),
        agent_overlay=overlay,
        config_defaults=config or RuntimeOverrides(),
        profile_routing_model=profile_routing_model,
        profile_routing_harness=profile_routing_harness,
        profile_policy_defaults=profile_policy_defaults or ResolvedExecutionPolicy(),
        profile_model_policies=profile_model_policies,
        profile_legacy_models=profile_legacy_models,
        profile_skills=(),
        resolved_alias_entry=resolved_alias,
        alias_catalog=resolved_catalog,
        configured_default_harness="claude",
        project_root="/repo",
        supported_execution_policy_fields=(
            supported_execution_policy_fields
            if supported_execution_policy_fields is not None
            else ("effort", "sandbox", "approval", "autocompact", "timeout")
        ),
    )


def test_render_provenance_only_includes_set_fields() -> None:
    provenance = FieldProvenance(
        model_source=ProvenanceLevel.CLI,
        effort_source=ProvenanceLevel.PROFILE_DEFAULT,
    )

    assert render_provenance(provenance) == {
        "model": "cli",
        "effort": "profile-default",
    }


def test_compiler_result_dry_run_dict_includes_provenance() -> None:
    result = CompilerResult(
        agent_name="coder",
        model="openai/gpt-5.4-mini",
        model_token="gptmini",
        harness="codex",
        execution_policy=ResolvedExecutionPolicy(
            effort="high",
            approval="auto",
            sandbox="workspace-write",
            autocompact=1000,
            timeout=30.0,
        ),
        skill_names=(),
        field_provenance=FieldProvenance(
            model_source=ProvenanceLevel.CLI,
            harness_source=ProvenanceLevel.ALIAS_DEFAULT,
            effort_source=ProvenanceLevel.PROFILE_MODEL_POLICY,
            approval_source=ProvenanceLevel.CONFIG_DEFAULT,
            sandbox_source=ProvenanceLevel.ENV,
            autocompact_source=ProvenanceLevel.PROFILE_DEFAULT,
            timeout_source=ProvenanceLevel.CLI,
        ),
        fallback_chain=(
            {
                "token": "gptmini",
                "position": 1,
                "override_summary": {"effort": "high"},
            },
        ),
        warnings=("warning-one", "warning-two"),
    )

    assert compiler_result_to_dry_run_dict(result) == {
        "agent": "coder",
        "model": "gptmini",
        "canonical_model": "openai/gpt-5.4-mini",
        "harness": "codex",
        "effort": "high",
        "approval": "auto",
        "sandbox": "workspace-write",
        "autocompact": 1000,
        "timeout": 30.0,
        "provenance": {
            "model": "cli",
            "harness": "alias-default",
            "effort": "profile-model-policy",
            "approval": "config-default",
            "sandbox": "env",
            "autocompact": "profile-default",
            "timeout": "cli",
        },
        "fallback_chain": [
            {
                "token": "gptmini",
                "position": 1,
                "override_summary": {"effort": "high"},
            }
        ],
        "warnings": ["warning-one", "warning-two"],
    }


def test_compile_launch_params_routing_precedence_cli_beats_overlay_and_profile() -> None:
    alias = _alias_entry()
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        overlay=AgentOverlayConfig(model="overlay-model"),
        profile_routing_model="profile-model",
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.model_token == "gptmini"
    assert result.model == "openai/gpt-5.4-mini"
    assert result.field_provenance.model_source is ProvenanceLevel.CLI


def test_compile_launch_params_routing_precedence_overlay_beats_profile() -> None:
    alias = _alias_entry()
    request = _request(
        overlay=AgentOverlayConfig(model="gptmini"),
        profile_routing_model="profile-model",
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.model_token == "gptmini"
    assert result.field_provenance.model_source is ProvenanceLevel.AGENT_OVERLAY_DEFAULT


def test_compile_launch_params_policy_precedence_cli_effort_wins() -> None:
    alias = _alias_entry()
    profile_rule = ModelPolicyRule(
        match_type="alias",
        match_value="gptmini",
        overrides={"effort": "high"},
    )
    request = _request(
        cli=RuntimeOverrides(model="gptmini", effort="xhigh"),
        overlay=AgentOverlayConfig(effort="medium"),
        profile_policy_defaults=ResolvedExecutionPolicy(effort="low"),
        profile_model_policies=(profile_rule,),
        config=RuntimeOverrides(effort="low"),
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.execution_policy.effort == "xhigh"
    assert result.field_provenance.effort_source is ProvenanceLevel.CLI


def test_compile_launch_params_policy_precedence_matched_policy_wins() -> None:
    alias = _alias_entry()
    profile_rule = ModelPolicyRule(
        match_type="alias",
        match_value="gptmini",
        overrides={"effort": "high"},
    )
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        overlay=AgentOverlayConfig(effort="medium"),
        profile_policy_defaults=ResolvedExecutionPolicy(effort="low"),
        profile_model_policies=(profile_rule,),
        config=RuntimeOverrides(effort="low"),
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.execution_policy.effort == "high"
    assert result.field_provenance.effort_source is ProvenanceLevel.PROFILE_MODEL_POLICY


def test_compile_launch_params_legacy_model_fallback_applies_without_model_policies() -> None:
    alias = _alias_entry()
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        overlay=AgentOverlayConfig(),
        profile_model_policies=(),
        profile_legacy_models={"gptmini": AgentModelEntry(effort="high")},
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.execution_policy.effort == "high"
    assert result.field_provenance.effort_source is ProvenanceLevel.PROFILE_MODEL_POLICY


def test_compile_launch_params_per_field_precedence_is_independent() -> None:
    alias = _alias_entry()
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        overlay=AgentOverlayConfig(effort="high"),
        config=RuntimeOverrides(approval="confirm"),
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.field_provenance.model_source is ProvenanceLevel.CLI
    assert result.field_provenance.effort_source is ProvenanceLevel.AGENT_OVERLAY_DEFAULT
    assert result.field_provenance.approval_source is ProvenanceLevel.CONFIG_DEFAULT


def test_compile_launch_params_timeout_excludes_overlay_and_profile_defaults() -> None:
    alias = _alias_entry()
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        overlay=AgentOverlayConfig(effort="high"),
        profile_policy_defaults=ResolvedExecutionPolicy(effort="low"),
        config=RuntimeOverrides(timeout=40.0),
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.execution_policy.timeout == 40.0
    assert result.field_provenance.timeout_source is ProvenanceLevel.CONFIG_DEFAULT


def test_compile_launch_params_three_state_empty_overlay_uses_profile_policy_list() -> None:
    alias = _alias_entry()
    profile_rule = ModelPolicyRule(
        match_type="alias",
        match_value="gptmini",
        overrides={"effort": "high"},
    )
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        overlay=AgentOverlayConfig(model_policies=()),
        profile_model_policies=(profile_rule,),
        profile_policy_defaults=ResolvedExecutionPolicy(effort="low"),
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.execution_policy.effort == "high"
    assert result.field_provenance.effort_source is ProvenanceLevel.PROFILE_MODEL_POLICY


def test_compile_launch_params_three_state_model_policies_overlay_prepends_profile() -> None:
    alias = _alias_entry()
    profile_rule = ModelPolicyRule(
        match_type="alias",
        match_value="gptmini",
        overrides={"effort": "high"},
    )
    overlay = AgentOverlayConfig(
        model_policies=(
            AgentOverlayModelPolicy(
                match_type="alias",
                match_value="gptmini",
                overrides={"effort": "xhigh"},
            ),
        )
    )
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        overlay=overlay,
        profile_model_policies=(profile_rule,),
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.execution_policy.effort == "xhigh"
    assert result.field_provenance.effort_source is ProvenanceLevel.AGENT_OVERLAY_POLICY


def test_compile_launch_params_overlay_prepends_before_profile_policies() -> None:
    alias = _alias_entry()
    profile_rule = ModelPolicyRule(
        match_type="alias",
        match_value="gptmini",
        overrides={"effort": "high"},
    )
    overlay = AgentOverlayConfig(
        model_policies=(
            AgentOverlayModelPolicy(
                match_type="alias",
                match_value="gptmini",
                overrides={"effort": "xhigh"},
            ),
        )
    )
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        overlay=overlay,
        profile_model_policies=(profile_rule,),
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.execution_policy.effort == "xhigh"
    assert result.field_provenance.effort_source is ProvenanceLevel.AGENT_OVERLAY_POLICY


def test_compile_launch_params_profile_rule_wins_when_overlay_rule_doesnt_match() -> None:
    alias = _alias_entry()
    profile_rule = ModelPolicyRule(
        match_type="alias",
        match_value="gptmini",
        overrides={"effort": "high"},
    )
    overlay = AgentOverlayConfig(
        model_policies=(
            AgentOverlayModelPolicy(
                match_type="alias",
                match_value="gpt55",
                overrides={"effort": "xhigh"},
            ),
        )
    )
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        overlay=overlay,
        profile_model_policies=(profile_rule,),
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.execution_policy.effort == "high"
    assert result.field_provenance.effort_source is ProvenanceLevel.PROFILE_MODEL_POLICY


def test_compile_launch_params_non_matching_overlay_rule_does_not_suppress_legacy_models() -> None:
    alias = _alias_entry()
    overlay = AgentOverlayConfig(
        model_policies=(
            AgentOverlayModelPolicy(
                match_type="alias",
                match_value="gpt55",
                overrides={"effort": "xhigh"},
            ),
        )
    )
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        overlay=overlay,
        profile_model_policies=(),
        profile_legacy_models={"gptmini": AgentModelEntry(effort="high")},
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.execution_policy.effort == "high"
    assert result.field_provenance.effort_source is ProvenanceLevel.PROFILE_MODEL_POLICY


def test_compile_launch_params_model_overrides_profile_policy_harness() -> None:
    alias = _alias_entry()
    overlay = AgentOverlayConfig(
        model="gptmini",
        model_policies=(
            AgentOverlayModelPolicy(
                match_type="alias",
                match_value="gpt55",
                overrides={"harness": "claude"},
            ),
        ),
    )
    profile_rule = ModelPolicyRule(
        match_type="alias",
        match_value="gptmini",
        overrides={"harness": "claude"},
    )
    request = _request(
        overlay=overlay,
        profile_model_policies=(profile_rule,),
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.model_policy_source is ProvenanceLevel.PROFILE_MODEL_POLICY
    assert result.harness == "codex"
    assert result.harness_provenance == "model-derived-override"
    assert result.field_provenance.harness_source is ProvenanceLevel.ALIAS_DEFAULT


def test_compile_launch_params_first_match_wins_no_ambiguity_error() -> None:
    alias = _alias_entry()
    first_glob = ModelPolicyRule(
        match_type="model-glob",
        match_value="openai/*",
        overrides={"effort": "medium"},
    )
    second_glob = ModelPolicyRule(
        match_type="model-glob",
        match_value="*/gpt-5.4-*",
        overrides={"effort": "high"},
    )
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        profile_model_policies=(first_glob, second_glob),
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.execution_policy.effort == "medium"
    assert result.field_provenance.effort_source is ProvenanceLevel.PROFILE_MODEL_POLICY


def test_compile_launch_params_ignores_unsupported_execution_fields() -> None:
    alias = _alias_entry()
    request = _request(
        cli=RuntimeOverrides(model="gptmini", timeout=10.0, effort="high"),
        env=RuntimeOverrides(timeout=20.0),
        config=RuntimeOverrides(timeout=30.0, approval="confirm"),
        alias_entry=alias,
        supported_execution_policy_fields=(
            "effort",
            "sandbox",
            "approval",
            "autocompact",
        ),
    )

    result = compile_launch_params(request)

    assert result.execution_policy.timeout is None
    assert result.execution_policy.approval == "confirm"
    assert result.field_provenance.timeout_source is ProvenanceLevel.UNSET


def test_compile_launch_params_harness_prefers_model_derived_over_profile() -> None:
    alias = _alias_entry()
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        profile_routing_harness="claude",
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.harness == "codex"
    assert result.harness_provenance == "model-derived-override"
    assert result.field_provenance.harness_source is ProvenanceLevel.ALIAS_DEFAULT


def test_compile_launch_params_child_cli_override_beats_parent_overlay() -> None:
    alias = _alias_entry()
    request = _request(
        cli=RuntimeOverrides(model="gptmini", effort="xhigh"),
        overlay=AgentOverlayConfig(effort="medium"),
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.execution_policy.effort == "xhigh"
    assert result.field_provenance.effort_source is ProvenanceLevel.CLI


class _StubCatalog:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def alias_map(self) -> dict[str, AliasEntry]:
        return {}

    def resolve_model(self, token: str) -> AliasEntry:
        raise ValueError(token)


class _MockHarnessRegistry:
    def get_subprocess_harness(self, harness_id: HarnessId) -> SubprocessHarness:
        _ = harness_id
        return cast("SubprocessHarness", object())


def test_resolve_policies_picks_up_overlay_from_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from meridian.lib.launch import policies as policies_module

    captured: dict[str, AgentOverlayConfig | None] = {}
    original_compile = policies_module.compile_launch_params

    def _capture_compile(request: CompilerRequest) -> CompilerResult:
        captured["overlay"] = request.agent_overlay
        return original_compile(request)

    def _load_agent_profile_with_fallback(**_: object) -> tuple[None, None]:
        return (None, None)

    def _materialize_harness(
        _result: object,
        harness_registry: HarnessRegistry,
    ) -> SimpleNamespace:
        _ = harness_registry
        return SimpleNamespace(adapter=object())

    def _resolve_skills_from_profile(**_: object) -> ResolvedSkills:
        return ResolvedSkills(skill_names=(), loaded_skills=(), missing_skills=())

    monkeypatch.setattr(policies_module, "compile_launch_params", _capture_compile)
    monkeypatch.setattr(
        policies_module,
        "load_agent_profile_with_fallback",
        _load_agent_profile_with_fallback,
    )
    monkeypatch.setattr(policies_module, "materialize_harness", _materialize_harness)
    monkeypatch.setattr(
        policies_module,
        "resolve_skills_from_profile",
        _resolve_skills_from_profile,
    )

    config = MeridianConfig.model_validate({"agents": {"coder": {"effort": "high"}}})
    overlay = config.agents["coder"]
    result = resolve_policies(
        catalog=cast("CatalogSession", _StubCatalog(tmp_path)),
        layers=(RuntimeOverrides(agent="coder"),),
        config_overrides=RuntimeOverrides(),
        config=config,
        harness_registry=cast("HarnessRegistry", _MockHarnessRegistry()),
    )

    assert captured["overlay"] == overlay
    assert result.execution_policy.effort == "high"
