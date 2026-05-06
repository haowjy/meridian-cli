from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, fields, is_dataclass
from enum import Enum
from typing import Any

import pytest
from pydantic import BaseModel

from meridian.lib.catalog.agent import AgentModelEntry, FanoutEntry, ModelPolicyRule
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.config.settings import AgentOverlayConfig
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.launch.compiler import (
    CompilerRequest,
    CompilerResult,
    FieldProvenance,
    ProvenanceLevel,
)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _alias_entry(alias: str = "gptmini") -> AliasEntry:
    return AliasEntry(
        alias=alias,
        model_id=ModelId("openai/gpt-5.4-mini"),
        resolved_harness=HarnessId.CODEX,
        default_effort="medium",
        default_autocompact=50,
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
