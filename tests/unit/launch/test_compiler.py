# qa-validated: orchestrator-opencode-fallback-runtime
from __future__ import annotations

from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.launch.compiler import (
    CompilerRequest,
    CompilerResult,
    ModelPolicyRule,
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


def test_compile_launch_params_routing_precedence_cli_beats_profile() -> None:
    alias = _alias_entry()
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
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
        profile_policy_defaults=ResolvedExecutionPolicy(effort="low"),
        profile_model_policies=(profile_rule,),
        config=RuntimeOverrides(effort="low"),
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.execution_policy.effort == "high"


def test_compile_launch_params_timeout_excludes_profile_defaults() -> None:
    alias = _alias_entry()
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        profile_policy_defaults=ResolvedExecutionPolicy(effort="low"),
        config=RuntimeOverrides(timeout=40.0),
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.execution_policy.timeout == 40.0


def test_compile_launch_params_model_derived_harness_beats_policy_harness_override() -> None:
    """Model-derived harness beats policy harness when model has higher precedence."""
    alias = _alias_entry()
    profile_rule = ModelPolicyRule(
        match_type="alias",
        match_value="gptmini",
        overrides={"harness": "claude"},
    )
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        profile_model_policies=(profile_rule,),
        alias_entry=alias,
    )

    result = compile_launch_params(request)

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


def test_resident_rearm_budget_resolves_cli_env_profile_config_precedence() -> None:
    supported = ("resident_rearm_budget",)
    profile = ResolvedExecutionPolicy(resident_rearm_budget=3)
    config = RuntimeOverrides(resident_rearm_budget=4)

    config_only = compile_launch_params(
        _request(config=config, supported_execution_policy_fields=supported)
    )
    profile_wins = compile_launch_params(
        _request(
            config=config,
            profile_policy_defaults=profile,
            supported_execution_policy_fields=supported,
        )
    )
    env_wins = compile_launch_params(
        _request(
            env=RuntimeOverrides(resident_rearm_budget=2),
            config=config,
            profile_policy_defaults=profile,
            supported_execution_policy_fields=supported,
        )
    )
    cli_wins = compile_launch_params(
        _request(
            cli=RuntimeOverrides(resident_rearm_budget=1),
            env=RuntimeOverrides(resident_rearm_budget=2),
            config=config,
            profile_policy_defaults=profile,
            supported_execution_policy_fields=supported,
        )
    )

    assert config_only.execution_policy.resident_rearm_budget == 4
    assert profile_wins.execution_policy.resident_rearm_budget == 3
    assert env_wins.execution_policy.resident_rearm_budget == 2
    assert cli_wins.execution_policy.resident_rearm_budget == 1
    assert ResolvedExecutionPolicy().resident_rearm_budget is None


def test_compile_launch_params_harness_prefers_model_derived_over_profile() -> None:
    alias = _alias_entry()
    request = _request(
        cli=RuntimeOverrides(model="gptmini"),
        profile_routing_harness="claude",
        alias_entry=alias,
    )

    result = compile_launch_params(request)

    assert result.harness == "codex"
