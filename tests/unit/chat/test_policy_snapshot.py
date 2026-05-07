from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meridian.lib.catalog.agent import AgentProfile
from meridian.lib.chat.policy import (
    CHAT_POLICY_SNAPSHOT_VERSION,
    ChatPolicySnapshot,
    ChatPromptDocumentSnapshot,
    ChatPromptInputsSnapshot,
    build_chat_backend_launch_plan,
    read_chat_policy_snapshot,
    snapshot_from_resolved_policy,
    write_chat_policy_snapshot,
)
from meridian.lib.core.domain import SkillContent
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.adapter import HarnessCapabilities
from meridian.lib.harness.codex import CodexAdapter
from meridian.lib.harness.ids import HarnessId
from meridian.lib.harness.launch_spec import CodexLaunchSpec
from meridian.lib.launch.compiler import FieldProvenance, ProvenanceLevel
from meridian.lib.launch.launch_types import CompositionWarning
from meridian.lib.launch.policies import ModelSelectionContext, ResolvedLaunchPolicy
from meridian.lib.launch.resolve import ResolvedSkills


def _profile(*, body: str = "PROFILE BODY", path: Path | None = None) -> AgentProfile:
    profile_path = path or Path("/repo/.mars/agents/reviewer.md")
    return AgentProfile(
        name="reviewer",
        description="review things",
        model="codex",
        harness="codex",
        skills=("profile-skill",),
        tools=("shell",),
        disallowed_tools=("rm",),
        mcp_tools=("github",),
        sandbox="workspace-write",
        effort="high",
        approval="auto",
        autocompact=33,
        body=body,
        path=profile_path,
        raw_content=f"# reviewer\n\n{body}",
    )


def _policy(*, profile: AgentProfile | None, supports_native_agents: bool) -> ResolvedLaunchPolicy:
    adapter = SimpleNamespace(
        capabilities=HarnessCapabilities(supports_native_agents=supports_native_agents),
        build_adhoc_agent_payload=lambda *, name, description, prompt: (
            f"NATIVE::{name}::{description}::{prompt.strip()}"
        ),
    )
    return ResolvedLaunchPolicy(
        profile=profile,
        model="gpt-5.3-codex",
        harness=HarnessId.CODEX,
        adapter=adapter,
        resolved_skills=ResolvedSkills(
            skill_names=("profile-skill", "cli-skill"),
            loaded_skills=(
                SkillContent(
                    name="profile-skill",
                    description="profile skill",
                    content="PROFILE SKILL CONTENT",
                    path="/repo/skills/profile/SKILL.md",
                ),
                SkillContent(
                    name="cli-skill",
                    description="cli skill",
                    content="CLI SKILL CONTENT",
                    path="/repo/skills/cli/SKILL.md",
                ),
            ),
            missing_skills=("missing-skill",),
        ),
        resolved_routing=RuntimeOverrides(model="codex", harness="codex", agent="reviewer"),
        resolved_execution_policy=RuntimeOverrides(
            effort="high",
            sandbox="workspace-write",
            approval="auto",
            autocompact=33,
        ),
        resolved_overrides=RuntimeOverrides(
            effort="high",
            sandbox="workspace-write",
            approval="auto",
            autocompact=33,
        ),
        field_provenance=FieldProvenance(
            model_source=ProvenanceLevel.CLI,
            harness_source=ProvenanceLevel.ALIAS_DEFAULT,
            effort_source=ProvenanceLevel.PROFILE_MODEL_POLICY,
            approval_source=ProvenanceLevel.ENV,
            sandbox_source=ProvenanceLevel.PROFILE_DEFAULT,
            autocompact_source=ProvenanceLevel.CONFIG_DEFAULT,
        ),
        model_selection=ModelSelectionContext(
            requested_token="codex",
            selected_model_token="codex",
            canonical_model_id="gpt-5.3-codex",
            mars_provided_harness=HarnessId.CODEX,
            resolved_entry=None,
            harness_provenance="mars-provided",
        ),
        warnings=(CompositionWarning(code="policy_warning", message="warn-1"),),
        alias_catalog=None,
    )


def test_snapshot_from_resolved_policy_captures_serializable_prompt_inputs_and_provenance(
    tmp_path: Path,
) -> None:
    profile_path = tmp_path / "reviewer.md"
    profile_path.write_text("# reviewer\n", encoding="utf-8")
    snapshot = snapshot_from_resolved_policy(
        _policy(
            profile=_profile(body="PROFILE BODY", path=profile_path),
            supports_native_agents=False,
        )
    )

    assert snapshot.snapshot_id
    assert snapshot.requested_model_token == "codex"
    assert snapshot.selected_model_token == "codex"
    assert snapshot.canonical_model_id == "gpt-5.3-codex"
    assert snapshot.harness == "codex"
    assert snapshot.harness_provenance == "mars-provided"
    assert snapshot.agent_name == "reviewer"
    assert snapshot.agent_profile_path == profile_path.resolve().as_posix()
    assert snapshot.skills == ("profile-skill", "cli-skill")
    assert snapshot.allowed_tools == ("shell",)
    assert snapshot.disallowed_tools == ("rm",)
    assert snapshot.mcp_tools == ("github",)
    assert snapshot.warnings == (
        CompositionWarning(code="policy_warning", message="warn-1"),
        CompositionWarning(
            code="missing_skills_warning",
            message=(
                "Warning: Skipped unavailable skills: missing-skill\n"
                "Expected: .mars/skills/missing-skill/SKILL.md\n"
                "Run `meridian mars sync` to install missing skills."
            ),
        ),
    )
    assert snapshot.field_provenance == {
        "model": "cli",
        "harness": "alias-default",
        "effort": "profile-model-policy",
        "approval": "env",
        "sandbox": "profile-default",
        "autocompact": "config-default",
    }
    assert snapshot.prompt_inputs.agent_profile_body == "PROFILE BODY"
    assert snapshot.prompt_inputs.adhoc_agent_payload == ""
    assert tuple(doc.logical_name for doc in snapshot.prompt_inputs.skill_documents) == (
        "profile-skill",
        "cli-skill",
    )
    assert tuple(doc.path for doc in snapshot.prompt_inputs.skill_documents) == (
        "/repo/skills/profile/SKILL.md",
        "/repo/skills/cli/SKILL.md",
    )
    assert snapshot.prompt_inputs.skill_documents[0].content.endswith("PROFILE SKILL CONTENT")
    assert snapshot.prompt_inputs.skill_documents[1].content.endswith("CLI SKILL CONTENT")


def test_snapshot_from_resolved_policy_uses_native_agent_payload_when_supported() -> None:
    snapshot = snapshot_from_resolved_policy(
        _policy(profile=_profile(body=" NATIVE BODY "), supports_native_agents=True)
    )

    assert snapshot.prompt_inputs.agent_profile_body == ""
    assert (
        snapshot.prompt_inputs.adhoc_agent_payload
        == "NATIVE::reviewer::review things::NATIVE BODY"
    )


def test_chat_policy_snapshot_write_read_roundtrip_preserves_immutable_prompt_inputs(
    tmp_path: Path,
) -> None:
    snapshot = ChatPolicySnapshot(
        snapshot_id="snap-1",
        requested_model_token="codex",
        selected_model_token="gpt-5.3-codex",
        canonical_model_id="gpt-5.3-codex",
        harness="codex",
        harness_provenance="mars-provided",
        agent_name="reviewer",
        agent_profile_path="/repo/.mars/agents/reviewer.md",
        skills=("profile-skill",),
        prompt_inputs=ChatPromptInputsSnapshot(
            skill_documents=(
                ChatPromptDocumentSnapshot(
                    kind="skill",
                    logical_name="profile-skill",
                    path="/repo/skills/profile/SKILL.md",
                    content="SNAPSHOT SKILL CONTENT",
                ),
            ),
            agent_profile_body="SNAPSHOT PROFILE BODY",
            adhoc_agent_payload="SNAPSHOT ADHOC",
        ),
        allowed_tools=("shell",),
        disallowed_tools=("rm",),
        mcp_tools=("github",),
        warnings=(CompositionWarning(code="policy_warning", message="warn-1"),),
        field_provenance={"model": "cli", "harness": "alias-default"},
    )
    path = tmp_path / "chat-policy.json"

    write_chat_policy_snapshot(path, snapshot)
    loaded = read_chat_policy_snapshot(path)

    assert loaded == snapshot


def test_chat_policy_snapshot_read_rejects_schema_version_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "chat-policy.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": CHAT_POLICY_SNAPSHOT_VERSION + 1,
                "snapshot_id": "snap-1",
                "requested_model_token": "",
                "selected_model_token": "",
                "canonical_model_id": "",
                "harness": "claude",
                "prompt_inputs": {
                    "skill_documents": [],
                    "agent_profile_body": "",
                    "adhoc_agent_payload": "",
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported chat policy snapshot schema version"):
        read_chat_policy_snapshot(path)


def test_build_chat_backend_launch_plan_uses_snapshot_and_reports_boundary_diagnostics(
    tmp_path: Path,
) -> None:
    skill_path = tmp_path / "skill.md"
    skill_path.write_text("CHANGED FILE CONTENT", encoding="utf-8")
    snapshot = ChatPolicySnapshot(
        snapshot_id="snap-immutable",
        requested_model_token="codex",
        selected_model_token="codex",
        canonical_model_id="gpt-5.3-codex",
        harness="codex",
        harness_provenance="mars-provided",
        effort="high",
        sandbox="workspace-write",
        approval="auto",
        autocompact=37,
        agent_name="reviewer",
        skills=("profile-skill",),
        prompt_inputs=ChatPromptInputsSnapshot(
            skill_documents=(
                ChatPromptDocumentSnapshot(
                    kind="skill",
                    logical_name="profile-skill",
                    path=str(skill_path),
                    content="SNAPSHOT SKILL CONTENT",
                ),
            ),
            agent_profile_body="SNAPSHOT PROFILE BODY",
            adhoc_agent_payload="SNAPSHOT ADHOC PAYLOAD",
        ),
        mcp_tools=("github",),
    )

    plan = build_chat_backend_launch_plan(
        snapshot=snapshot,
        initial_prompt="Investigate the issue",
        spawn_id=SpawnId("chat-s1"),
        adapter=CodexAdapter(),
        project_root=tmp_path,
        runtime_root=tmp_path / "runtime",
    )

    assert plan.harness_id == HarnessId.CODEX
    assert isinstance(plan.spec, CodexLaunchSpec)
    assert plan.spec.model == "gpt-5.3-codex"
    assert plan.spec.base_instructions == "SNAPSHOT ADHOC PAYLOAD"
    assert "SNAPSHOT SKILL CONTENT" in (plan.spec.developer_instructions or "")
    assert "SNAPSHOT PROFILE BODY" in (plan.spec.developer_instructions or "")
    assert "CHANGED FILE CONTENT" not in (plan.spec.developer_instructions or "")
    assert plan.connection_config.env_overrides["MERIDIAN_HARNESS"] == "codex"
    assert plan.connection_config.env_overrides["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "37"
    assert plan.configured_payload == {
        "harness": "codex",
        "model": "gpt-5.3-codex",
        "requested_model_token": "codex",
        "selected_model_token": "codex",
        "harness_provenance": "mars-provided",
        "policy_snapshot_id": "snap-immutable",
        "autocompact": 37,
    }
