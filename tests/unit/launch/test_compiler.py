# qa-validated: orchestrator-opencode-fallback-runtime
from __future__ import annotations

from meridian.lib.catalog.agent import ModelPolicyRule
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.config.settings import AgentOverlayConfig, AgentOverlayModelPolicy
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.launch.compiler import (
    CompilerRequest,
    CompilerResult,
    compile_launch_params,
    compiler_result_to_dry_run_dict,
)


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
        profile_skills=(),
        resolved_alias_entry=resolved_alias,
        alias_catalog=resolved_catalog,
        project_root="/repo",
        supported_execution_policy_fields=(
            supported_execution_policy_fields
            if supported_execution_policy_fields is not None
            else ("effort", "sandbox", "approval", "autocompact", "timeout")
        ),
    )


def test_compiler_result_dry_run_dict_includes_fallback_chain_and_warnings() -> None:
    result = CompilerResult(
        agent_name="coder",
        model="openai/gpt-5.4-mini",
        model_token="gptmini",
        harness="codex",
        execution_policy=ResolvedExecutionPolicy(effort="high"),
        skill_names=(),
        fallback_chain=(
            {"token": "gptmini", "position": 1, "override_summary": {"effort": "high"}},
        ),
        warnings=("warning-one",),
    )

    output = compiler_result_to_dry_run_dict(result)

    assert output["model"] == "gptmini"  # model_token used when set
    assert output["fallback_chain"] == [
        {"token": "gptmini", "position": 1, "override_summary": {"effort": "high"}}
    ]
    assert output["warnings"] == ["warning-one"]


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


def test_compile_launch_params_model_derived_harness_beats_policy_harness_override() -> None:
    """Model-derived harness (from alias catalog) wins over a policy rule that sets harness
    to a different value when the model was set at a higher-precedence tier than the harness."""
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

    # alias catalog says gptmini → codex; policy tried to set claude but model-derived wins
    assert result.harness == "codex"


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


def test_compile_launch_params_harness_prefers_model_derived_over_profile() -> None:
    alias = _alias_entry()
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        profile_routing_harness="claude",
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.harness == "codex"
