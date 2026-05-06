from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, fields, is_dataclass
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import BaseModel

from meridian.lib.catalog.agent import AgentModelEntry, FanoutEntry, ModelPolicyRule
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.config.settings import (
    AgentOverlayConfig,
    AgentOverlayModelPolicy,
    MeridianConfig,
)
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


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in cast("list[Any] | tuple[Any, ...]", value)]
    if isinstance(value, dict):
        return {
            str(key): _jsonable(item)
            for key, item in cast("dict[object, Any]", value).items()
        }
    return value


def _alias_entry(alias: str = "gptmini") -> AliasEntry:
    return AliasEntry(
        alias=alias,
        model_id=ModelId("openai/gpt-5.4-mini"),
        resolved_harness=HarnessId.CODEX,
        default_effort="medium",
        default_autocompact=50,
    )


def _request(
    *,
    cli: RuntimeOverrides | None = None,
    env: RuntimeOverrides | None = None,
    overlay: AgentOverlayConfig | None = None,
    config: RuntimeOverrides | None = None,
    profile_routing_model: str | None = None,
    profile_routing_harness: str | None = None,
    profile_policy_effort: str | None = None,
    profile_policy_approval: str | None = None,
    profile_policy_sandbox: str | None = None,
    profile_policy_autocompact: int | None = None,
    profile_model_policies: tuple[ModelPolicyRule, ...] | None = None,
    profile_legacy_models: dict[str, AgentModelEntry] | None = None,
    profile_skills: tuple[str, ...] = (),
    alias_entry: AliasEntry | None = None,
    alias_catalog: dict[str, AliasEntry] | None = None,
) -> CompilerRequest:
    resolved_alias = alias_entry
    resolved_catalog = alias_catalog
    if resolved_catalog is None:
        resolved_catalog = {alias_entry.alias: alias_entry} if alias_entry is not None else {}
    return CompilerRequest(
        requested_agent="coder",
        requested_model=None,
        cli_overrides=cli or RuntimeOverrides(),
        env_overrides=env or RuntimeOverrides(),
        agent_overlay=overlay,
        config_defaults=config or RuntimeOverrides(),
        profile_routing_model=profile_routing_model,
        profile_routing_harness=profile_routing_harness,
        profile_policy_effort=profile_policy_effort,
        profile_policy_approval=profile_policy_approval,
        profile_policy_sandbox=profile_policy_sandbox,
        profile_policy_autocompact=profile_policy_autocompact,
        profile_model_policies=profile_model_policies,
        profile_legacy_models=profile_legacy_models,
        profile_fanout=(),
        profile_skills=profile_skills,
        resolved_alias_entry=resolved_alias,
        alias_catalog=resolved_catalog,
        configured_default_harness="claude",
        project_root="/repo",
    )


def test_compiler_request_and_result_construct_with_all_fields() -> None:
    model_policy = ModelPolicyRule(
        match_type="alias",
        match_value="gptmini",
        overrides={"effort": "high"},
    )
    legacy_model = AgentModelEntry(effort="low", autocompact=25)
    fanout = FanoutEntry(entry_type="alias", value="gptmini")
    alias_entry = _alias_entry()

    request = CompilerRequest(
        requested_agent="coder",
        requested_model="gptmini",
        cli_overrides=RuntimeOverrides(model="gptmini", approval="auto"),
        env_overrides=RuntimeOverrides(effort="medium"),
        agent_overlay=AgentOverlayConfig(effort="high", autocompact=80),
        config_defaults=RuntimeOverrides(model="codex", sandbox="workspace-write"),
        profile_routing_model="gptmini",
        profile_routing_harness="codex",
        profile_policy_effort="high",
        profile_policy_approval="confirm",
        profile_policy_sandbox="workspace-write",
        profile_policy_autocompact=70,
        profile_model_policies=(model_policy,),
        profile_legacy_models={"gptmini": legacy_model},
        profile_fanout=(fanout,),
        profile_skills=("dev-principles",),
        resolved_alias_entry=alias_entry,
        alias_catalog={"gptmini": alias_entry},
        configured_default_harness="codex",
        project_root="/repo",
    )

    result = CompilerResult(
        agent_name="coder",
        profile_found=True,
        model="openai/gpt-5.4-mini",
        model_token="gptmini",
        harness="codex",
        effort="high",
        approval="auto",
        sandbox="workspace-write",
        autocompact=70,
        timeout=30.0,
        skill_names=("dev-principles",),
        tools=("shell",),
        disallowed_tools=("rm",),
        mcp_tools=("github",),
        model_selection_requested_token="gptmini",
        model_selection_canonical_id="openai/gpt-5.4-mini",
        harness_provenance="mars-provided",
        field_provenance=FieldProvenance(
            model_source=ProvenanceLevel.CLI,
            harness_source=ProvenanceLevel.ALIAS_DEFAULT,
            effort_source=ProvenanceLevel.PROFILE_MODEL_POLICY,
            approval_source=ProvenanceLevel.CLI,
            sandbox_source=ProvenanceLevel.CONFIG_DEFAULT,
            autocompact_source=ProvenanceLevel.PROFILE_DEFAULT,
            timeout_source=ProvenanceLevel.CONFIG_DEFAULT,
        ),
        warnings=("warning",),
        fallback_applied=True,
        fallback_model="gptmini",
    )

    assert request.requested_agent == "coder"
    assert request.profile_model_policies == (model_policy,)
    assert result.field_provenance.model_source is ProvenanceLevel.CLI
    assert result.fallback_model == "gptmini"


def test_field_provenance_defaults_to_unset_for_all_fields() -> None:
    provenance = FieldProvenance()

    for field in fields(FieldProvenance):
        assert getattr(provenance, field.name) is ProvenanceLevel.UNSET


def test_compiler_types_are_frozen() -> None:
    request = CompilerRequest(
        requested_agent=None,
        requested_model=None,
        cli_overrides=RuntimeOverrides(),
        env_overrides=RuntimeOverrides(),
        agent_overlay=None,
        config_defaults=RuntimeOverrides(),
        profile_routing_model=None,
        profile_routing_harness=None,
        profile_policy_effort=None,
        profile_policy_approval=None,
        profile_policy_sandbox=None,
        profile_policy_autocompact=None,
        profile_model_policies=None,
        profile_legacy_models=None,
        profile_fanout=None,
        profile_skills=(),
        resolved_alias_entry=None,
        alias_catalog={},
    )
    result = CompilerResult(
        agent_name=None,
        profile_found=False,
        model="",
        model_token="",
        harness="claude",
        effort=None,
        approval=None,
        sandbox=None,
        autocompact=None,
        timeout=None,
        skill_names=(),
    )
    provenance = FieldProvenance()

    with pytest.raises(FrozenInstanceError):
        request.requested_agent = "coder"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.model = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        provenance.model_source = ProvenanceLevel.CLI  # type: ignore[misc]


def test_compiler_types_are_json_serializable_with_dataclass_to_dict_pattern() -> None:
    alias_entry = _alias_entry()
    request = CompilerRequest(
        requested_agent="coder",
        requested_model="gptmini",
        cli_overrides=RuntimeOverrides(model="gptmini"),
        env_overrides=RuntimeOverrides(effort="low"),
        agent_overlay=AgentOverlayConfig(approval="auto"),
        config_defaults=RuntimeOverrides(sandbox="workspace-write"),
        profile_routing_model=None,
        profile_routing_harness=None,
        profile_policy_effort=None,
        profile_policy_approval=None,
        profile_policy_sandbox=None,
        profile_policy_autocompact=None,
        profile_model_policies=(),
        profile_legacy_models={},
        profile_fanout=(),
        profile_skills=("dev-principles",),
        resolved_alias_entry=alias_entry,
        alias_catalog={"gptmini": alias_entry},
        project_root="/repo",
    )
    result = CompilerResult(
        agent_name="coder",
        profile_found=True,
        model="openai/gpt-5.4-mini",
        model_token="gptmini",
        harness="codex",
        effort="low",
        approval="auto",
        sandbox="workspace-write",
        autocompact=None,
        timeout=None,
        skill_names=("dev-principles",),
        field_provenance=FieldProvenance(model_source=ProvenanceLevel.CLI),
    )

    request_payload = _jsonable(request)
    result_payload = _jsonable(result)

    json.dumps(request_payload, sort_keys=True)
    json.dumps(result_payload, sort_keys=True)
    assert request_payload["cli_overrides"]["model"] == "gptmini"
    assert result_payload["field_provenance"]["model_source"] == "cli"


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
        profile_found=True,
        model="openai/gpt-5.4-mini",
        model_token="gptmini",
        harness="codex",
        effort="high",
        approval="auto",
        sandbox="workspace-write",
        autocompact=70,
        timeout=30.0,
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
        fallback_applied=True,
        fallback_model="gptmini",
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
        "autocompact": 70,
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
        "fallback_model": "gptmini",
        "warnings": ["warning-one", "warning-two"],
    }


def test_compiler_result_dry_run_dict_minimal() -> None:
    result = CompilerResult(
        agent_name=None,
        profile_found=False,
        model="openai/gpt-5.4-mini",
        model_token="",
        harness="codex",
        effort=None,
        approval=None,
        sandbox=None,
        autocompact=None,
        timeout=None,
        skill_names=(),
    )

    assert compiler_result_to_dry_run_dict(result) == {
        "agent": None,
        "model": "openai/gpt-5.4-mini",
        "canonical_model": "openai/gpt-5.4-mini",
        "harness": "codex",
        "provenance": {},
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
        profile_policy_effort="low",
        profile_model_policies=(profile_rule,),
        config=RuntimeOverrides(effort="low"),
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.effort == "xhigh"
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
        profile_policy_effort="low",
        profile_model_policies=(profile_rule,),
        config=RuntimeOverrides(effort="low"),
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.effort == "high"
    assert result.field_provenance.effort_source is ProvenanceLevel.PROFILE_MODEL_POLICY


def test_compile_launch_params_three_state_model_policies_inherit_profile() -> None:
    alias = _alias_entry()
    profile_rule = ModelPolicyRule(
        match_type="alias",
        match_value="gptmini",
        overrides={"effort": "high"},
    )
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        overlay=AgentOverlayConfig(),
        profile_model_policies=(profile_rule,),
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.effort == "high"
    assert result.field_provenance.effort_source is ProvenanceLevel.PROFILE_MODEL_POLICY


def test_compile_launch_params_three_state_empty_overlay_uses_empty_policy_list() -> None:
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
        profile_policy_effort="low",
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.effort == "low"
    assert result.field_provenance.effort_source is ProvenanceLevel.PROFILE_DEFAULT


def test_compile_launch_params_three_state_model_policies_overlay_replaces_profile() -> None:
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

    assert result.effort == "xhigh"
    assert result.field_provenance.effort_source is ProvenanceLevel.AGENT_OVERLAY_POLICY


def test_compile_launch_params_timeout_v1_excludes_overlay_defaults() -> None:
    alias = _alias_entry()
    rule = ModelPolicyRule(
        match_type="alias",
        match_value="gptmini",
        overrides={"timeout": 20.0},
    )
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        overlay=AgentOverlayConfig(effort="high"),
        profile_model_policies=(rule,),
        config=RuntimeOverrides(timeout=40.0),
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.timeout == 20.0
    assert result.field_provenance.timeout_source is ProvenanceLevel.PROFILE_MODEL_POLICY


def test_compile_launch_params_legacy_models_apply_when_overlay_absent() -> None:
    alias = _alias_entry()
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        overlay=AgentOverlayConfig(),
        profile_model_policies=(),
        profile_legacy_models={"gptmini": AgentModelEntry(effort="high")},
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.effort == "high"
    assert result.field_provenance.effort_source is ProvenanceLevel.PROFILE_MODEL_POLICY


def test_compile_launch_params_legacy_models_ignored_when_overlay_sets_empty_policy_list() -> None:
    alias = _alias_entry()
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        overlay=AgentOverlayConfig(model_policies=()),
        profile_model_policies=(),
        profile_legacy_models={"gptmini": AgentModelEntry(effort="high")},
        profile_policy_effort="low",
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.effort == "low"
    assert result.field_provenance.effort_source is ProvenanceLevel.PROFILE_DEFAULT


def test_compile_launch_params_per_field_independence() -> None:
    alias = _alias_entry()
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        overlay=AgentOverlayConfig(effort="high"),
        config=RuntimeOverrides(approval="confirm"),
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.model_token == "gptmini"
    assert result.field_provenance.model_source is ProvenanceLevel.CLI
    assert result.effort == "high"
    assert result.field_provenance.effort_source is ProvenanceLevel.AGENT_OVERLAY_DEFAULT
    assert result.approval == "confirm"
    assert result.field_provenance.approval_source is ProvenanceLevel.CONFIG_DEFAULT


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


def test_config_snapshot_includes_agents_overlay() -> None:
    """Agent overlays survive config snapshot serialization for child spawns."""
    config = MeridianConfig.model_validate(
        {
            "agents": {
                "coder": {
                    "model": "gptmini",
                    "effort": "high",
                    "model_policies": (
                        {
                            "match_type": "alias",
                            "match_value": "gptmini",
                            "overrides": {"approval": "auto"},
                        },
                    ),
                }
            }
        }
    )

    snapshot = config.model_dump(mode="python")
    restored = MeridianConfig.model_validate(snapshot)
    overlay = restored.agents["coder"]

    assert overlay.model == "gptmini"
    assert overlay.effort == "high"
    assert overlay.model_policies is not None
    assert overlay.model_policies[0].match_value == "gptmini"
    assert overlay.model_policies[0].overrides == {"approval": "auto"}


def test_no_agent_selected_produces_no_overlay_effect() -> None:
    """When no agent is selected, compiler behaves as if overlay doesn't exist."""
    alias = _alias_entry()
    request = CompilerRequest(
        requested_agent=None,
        requested_model=None,
        cli_overrides=RuntimeOverrides(model="gptmini"),
        env_overrides=RuntimeOverrides(),
        agent_overlay=None,
        config_defaults=RuntimeOverrides(effort="low"),
        profile_routing_model=None,
        profile_routing_harness=None,
        profile_policy_effort=None,
        profile_policy_approval=None,
        profile_policy_sandbox=None,
        profile_policy_autocompact=None,
        profile_model_policies=None,
        profile_legacy_models=None,
        profile_fanout=None,
        profile_skills=(),
        resolved_alias_entry=alias,
        alias_catalog={"gptmini": alias},
    )

    result = compile_launch_params(request)

    assert result.effort == "low"
    assert result.field_provenance.effort_source is ProvenanceLevel.CONFIG_DEFAULT


def test_overlay_on_missing_profile_has_no_effect() -> None:
    """Overlay on an agent with no installed profile is stored but doesn't crash."""
    alias = _alias_entry()
    overlay = AgentOverlayConfig(
        model_policies=(
            AgentOverlayModelPolicy(
                match_type="alias",
                match_value="gptmini",
                overrides={"effort": "high"},
            ),
        )
    )
    request = CompilerRequest(
        requested_agent="missing-profile-agent",
        requested_model=None,
        cli_overrides=RuntimeOverrides(),
        env_overrides=RuntimeOverrides(),
        agent_overlay=overlay,
        config_defaults=RuntimeOverrides(effort="low"),
        profile_routing_model=None,
        profile_routing_harness=None,
        profile_policy_effort=None,
        profile_policy_approval=None,
        profile_policy_sandbox=None,
        profile_policy_autocompact=None,
        profile_model_policies=None,
        profile_legacy_models=None,
        profile_fanout=None,
        profile_skills=(),
        resolved_alias_entry=alias,
        alias_catalog={"gptmini": alias},
    )

    result = compile_launch_params(request)

    assert result.effort == "low"
    assert result.field_provenance.effort_source is ProvenanceLevel.CONFIG_DEFAULT


def test_child_cli_overrides_beat_parent_overlay() -> None:
    """Explicit CLI overrides in child spawn beat overlay values."""
    alias = _alias_entry()
    request = _request(
        cli=RuntimeOverrides(model="gptmini", effort="xhigh"),
        overlay=AgentOverlayConfig(effort="medium"),
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.effort == "xhigh"
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
    """resolve_policies reads agent overlay from config.agents when agent is selected."""
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
    assert result.resolved_overrides.effort == "high"
