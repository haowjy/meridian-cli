"""Shared chat launch-policy snapshot and acquisition plan helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.config.project_paths import ProjectConfigPaths
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.adapter import SubprocessHarness
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.harness.ids import HarnessId
from meridian.lib.launch.composition import ComposedLaunchContent, PromptDocument
from meridian.lib.launch.context import (
    build_child_runtime_env_overrides,
    materialize_launch_artifacts,
    prepare_prompt_payload,
)
from meridian.lib.launch.launch_types import CompositionWarning, ResolvedLaunchSpec
from meridian.lib.launch.policies import ResolvedLaunchPolicy
from meridian.lib.launch.prompt import compose_skill_prompt_documents
from meridian.lib.launch.resolve import format_missing_skills_warning, resolve_profile_path
from meridian.lib.state.atomic import atomic_write_text

CHAT_POLICY_SNAPSHOT_VERSION = 1


class ChatPromptDocumentSnapshot(BaseModel):
    """Serializable snapshot of one prompt document."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["skill", "bootstrap"]
    logical_name: str
    path: str
    content: str

    def to_prompt_document(self) -> PromptDocument:
        return PromptDocument(
            kind=self.kind,
            logical_name=self.logical_name,
            path=self.path,
            content=self.content,
        )


class ChatPromptInputsSnapshot(BaseModel):
    """Immutable prompt inputs persisted for chat reacquisition."""

    model_config = ConfigDict(frozen=True)

    skill_documents: tuple[ChatPromptDocumentSnapshot, ...] = ()
    agent_profile_body: str = ""
    adhoc_agent_payload: str = ""


class ChatPolicySnapshot(BaseModel):
    """Durable per-chat policy snapshot used for first acquire + restart."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = CHAT_POLICY_SNAPSHOT_VERSION
    snapshot_id: str

    requested_model_token: str = ""
    selected_model_token: str = ""
    canonical_model_id: str = ""
    harness: str
    harness_provenance: str = ""

    effort: str | None = None
    sandbox: str | None = None
    approval: str | None = None
    autocompact: int | None = None

    agent_name: str | None = None
    agent_profile_path: str | None = None
    skills: tuple[str, ...] = ()
    prompt_inputs: ChatPromptInputsSnapshot

    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    mcp_tools: tuple[str, ...] = ()

    warnings: tuple[CompositionWarning, ...] = ()
    field_provenance: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class ChatBackendLaunchPlan:
    """Prepared chat backend launch inputs for one acquisition."""

    harness_id: HarnessId
    connection_config: ConnectionConfig
    spec: ResolvedLaunchSpec
    configured_payload: dict[str, object]


def _supports_launch_autocompact(harness_id: HarnessId) -> bool:
    """Return whether launch-time autocompact env override is supported."""

    return harness_id == HarnessId.CLAUDE


def snapshot_from_resolved_policy(policy: ResolvedLaunchPolicy) -> ChatPolicySnapshot:
    """Build a durable immutable chat policy snapshot from shared resolution."""

    profile = policy.profile
    adapter = policy.adapter
    skill_docs = tuple(
        ChatPromptDocumentSnapshot(
            kind=document.kind,
            logical_name=document.logical_name,
            path=document.path,
            content=document.content,
        )
        for document in compose_skill_prompt_documents(policy.resolved_skills.loaded_skills)
    )

    agent_profile_body = ""
    adhoc_agent_payload = ""
    if profile is not None:
        if adapter.capabilities.supports_native_agents:
            adhoc_agent_payload = adapter.build_adhoc_agent_payload(
                name=profile.name,
                description=profile.description,
                prompt=profile.body,
            )
        elif profile.body.strip():
            agent_profile_body = profile.body.strip()
    prompt_inputs = ChatPromptInputsSnapshot(
        skill_documents=skill_docs,
        agent_profile_body=agent_profile_body,
        adhoc_agent_payload=adhoc_agent_payload,
    )

    model_selection = policy.model_selection
    warnings = list(policy.warnings)
    missing_skills_warning = format_missing_skills_warning(policy.resolved_skills.missing_skills)
    if missing_skills_warning:
        warnings.append(
            CompositionWarning(
                code="missing_skills_warning",
                message=missing_skills_warning,
            )
        )

    snapshot = ChatPolicySnapshot(
        snapshot_id="",
        schema_version=CHAT_POLICY_SNAPSHOT_VERSION,
        requested_model_token=(
            model_selection.requested_token if model_selection is not None else policy.model
        ),
        selected_model_token=(
            model_selection.selected_model_token if model_selection is not None else policy.model
        ),
        canonical_model_id=(
            model_selection.canonical_model_id if model_selection is not None else policy.model
        ),
        harness=policy.harness.value,
        harness_provenance=(
            model_selection.harness_provenance if model_selection is not None else ""
        ),
        effort=policy.resolved_overrides.effort,
        sandbox=policy.resolved_overrides.sandbox,
        approval=policy.resolved_overrides.approval,
        autocompact=policy.resolved_overrides.autocompact,
        agent_name=profile.name if profile is not None else None,
        agent_profile_path=resolve_profile_path(profile),
        skills=policy.resolved_skills.skill_names,
        prompt_inputs=prompt_inputs,
        allowed_tools=profile.tools if profile is not None else (),
        disallowed_tools=profile.disallowed_tools if profile is not None else (),
        mcp_tools=profile.mcp_tools if profile is not None else (),
        warnings=tuple(warnings),
        field_provenance={
            key: value
            for key, value in {
                "model": policy.field_provenance.model_source.value,
                "harness": policy.field_provenance.harness_source.value,
                "effort": policy.field_provenance.effort_source.value,
                "approval": policy.field_provenance.approval_source.value,
                "sandbox": policy.field_provenance.sandbox_source.value,
                "autocompact": policy.field_provenance.autocompact_source.value,
                "timeout": policy.field_provenance.timeout_source.value,
            }.items()
            if value and value != "unset"
        },
    )
    payload_json = json.dumps(
        snapshot.model_dump(mode="json", exclude={"snapshot_id"}),
        sort_keys=True,
        separators=(",", ":"),
    )
    snapshot_id = sha256(payload_json.encode("utf-8")).hexdigest()
    return snapshot.model_copy(update={"snapshot_id": snapshot_id})


def write_chat_policy_snapshot(path: Path, snapshot: ChatPolicySnapshot) -> None:
    """Persist chat policy snapshot atomically."""

    atomic_write_text(path, json.dumps(snapshot.model_dump(mode="json"), sort_keys=True) + "\n")


def read_chat_policy_snapshot(path: Path) -> ChatPolicySnapshot:
    """Load one persisted chat policy snapshot."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid chat policy snapshot: {path}")
    snapshot = ChatPolicySnapshot.model_validate(payload)
    if snapshot.schema_version != CHAT_POLICY_SNAPSHOT_VERSION:
        raise ValueError(
            f"unsupported chat policy snapshot schema version {snapshot.schema_version}; "
            f"expected {CHAT_POLICY_SNAPSHOT_VERSION}"
        )
    return snapshot


def build_chat_backend_launch_plan(
    *,
    snapshot: ChatPolicySnapshot,
    initial_prompt: str,
    spawn_id: SpawnId,
    adapter: SubprocessHarness,
    project_root: Path,
    runtime_root: Path,
) -> ChatBackendLaunchPlan:
    """Build chat launch plan from immutable snapshot + current user prompt."""

    composed = ComposedLaunchContent(
        supplemental_documents=tuple(
            document.to_prompt_document() for document in snapshot.prompt_inputs.skill_documents
        ),
        agent_profile_body=snapshot.prompt_inputs.agent_profile_body,
        report_instruction="",
        inventory_prompt="",
        context_prompt="",
        passthrough_system_fragments=(),
        user_task_prompt=initial_prompt,
        reference_items=(),
        prior_output="",
    )
    projected = adapter.project_content(composed)
    harness_id = HarnessId(snapshot.harness)
    prompt_payload = prepare_prompt_payload(
        adhoc_agent_payload=snapshot.prompt_inputs.adhoc_agent_payload,
        projected_content=projected,
    )
    materialized = materialize_launch_artifacts(
        harness=adapter,
        prompt=projected.user_turn_content.strip() or initial_prompt,
        model=snapshot.canonical_model_id,
        effort=snapshot.effort,
        skills=snapshot.skills,
        agent=snapshot.agent_name,
        prompt_payload=prompt_payload,
        project_root=project_root.as_posix(),
        mcp_tools=snapshot.mcp_tools,
        sandbox=snapshot.sandbox,
        allowed_tools=snapshot.allowed_tools,
        disallowed_tools=snapshot.disallowed_tools,
        approval=snapshot.approval,
    )
    runtime_env = build_child_runtime_env_overrides(
        project_paths=ProjectConfigPaths(
            project_root=project_root.resolve(),
            execution_cwd=project_root.resolve(),
        ),
        runtime_root=runtime_root,
        child_spawn_id=str(spawn_id),
    )
    runtime_env["MERIDIAN_HARNESS"] = snapshot.harness
    autocompact_supported = _supports_launch_autocompact(harness_id)
    if snapshot.autocompact is not None and autocompact_supported:
        runtime_env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(snapshot.autocompact)

    config = ConnectionConfig(
        spawn_id=spawn_id,
        harness_id=harness_id,
        prompt=materialized.run_params.prompt,
        project_root=project_root,
        env_overrides=runtime_env,
        system=materialized.run_params.appended_system_prompt,
    )
    configured_payload: dict[str, object] = {
        "harness": snapshot.harness,
        "model": snapshot.canonical_model_id,
        "requested_model_token": snapshot.requested_model_token,
        "selected_model_token": snapshot.selected_model_token,
        "harness_provenance": snapshot.harness_provenance,
        "policy_snapshot_id": snapshot.snapshot_id,
    }
    if snapshot.autocompact is not None and autocompact_supported:
        configured_payload["autocompact"] = snapshot.autocompact
    return ChatBackendLaunchPlan(
        harness_id=config.harness_id,
        connection_config=config,
        spec=materialized.spec,
        configured_payload=configured_payload,
    )


def default_chat_policy_snapshot(
    *,
    harness: HarnessId = HarnessId.CLAUDE,
    model: str = "",
) -> ChatPolicySnapshot:
    """Return a minimal default snapshot for tests/unconfigured runtime."""

    return ChatPolicySnapshot(
        snapshot_id="default",
        canonical_model_id=model,
        selected_model_token=model,
        requested_model_token=model,
        harness=harness.value,
        prompt_inputs=ChatPromptInputsSnapshot(),
    )


__all__ = [
    "CHAT_POLICY_SNAPSHOT_VERSION",
    "ChatBackendLaunchPlan",
    "ChatPolicySnapshot",
    "ChatPromptDocumentSnapshot",
    "ChatPromptInputsSnapshot",
    "build_chat_backend_launch_plan",
    "default_chat_policy_snapshot",
    "read_chat_policy_snapshot",
    "snapshot_from_resolved_policy",
    "write_chat_policy_snapshot",
]
