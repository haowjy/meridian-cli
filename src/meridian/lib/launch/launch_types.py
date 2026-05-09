"""Leaf launch contracts shared across launch policy, content, and runners."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, model_validator

from meridian.lib.core.execution_policy import ResolvedExecutionPolicy as ResolvedExecutionPolicy
from meridian.lib.core.overrides import (
    RuntimeOverrides,
)

if TYPE_CHECKING:
    from meridian.lib.harness.ids import HarnessId
    from meridian.lib.launch.composition import ProjectedContent
    from meridian.lib.launch.reference import ReferenceItem
    from meridian.lib.launch.run_inputs import ResolvedRunInputs
    from meridian.lib.safety.permissions import PermissionConfig


class TerminalSurfaceMode(StrEnum):
    """How Meridian surfaces an interactive harness terminal."""

    PTY_MEDIATED = "pty_mediated"
    NATIVE_INHERIT = "native_inherit"


class CompositionWarning(BaseModel):
    """User-visible warning emitted by launch composition stages."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    code: str
    message: str
    detail: dict[str, str] | None = None

    @model_validator(mode="before")
    @classmethod
    def _freeze_detail(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        raw_value = cast("dict[Any, Any]", value)
        payload: dict[str, Any] = {str(key): item for key, item in raw_value.items()}
        detail = payload.get("detail")
        if isinstance(detail, dict):
            raw_detail = cast("dict[Any, Any]", detail)
            payload["detail"] = {str(key): str(item) for key, item in raw_detail.items()}
        return payload


@runtime_checkable
class PermissionResolver(Protocol):
    """Transport-neutral permission intent resolver."""

    @property
    def config(self) -> PermissionConfig: ...

    def resolve_flags(self) -> tuple[str, ...]:
        return ()


class ResolvedLaunchSpec(BaseModel):
    """Transport-neutral resolved configuration for one harness launch."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # Identity
    model: str | None = None

    # Execution parameters
    effort: str | None = None
    prompt: str = ""

    # Session continuity
    continue_session_id: str | None = None
    continue_fork: bool = False

    # Permissions
    permission_resolver: PermissionResolver

    # Passthrough args forwarded verbatim to the harness process.
    extra_args: tuple[str, ...] = ()

    # Interactive mode
    interactive: bool = False

    # Harness-agnostic MCP tool configuration.
    mcp_tools: tuple[str, ...] = ()

    # First-class workspace root projection — harness-agnostic, deduplicated.
    projected_roots: tuple[Path, ...] = ()

    # Adapter-local projections: fields used by specific harness adapters.
    # All are optional and default to their zero values so unused adapters
    # don't need to set them.

    # Claude + OpenCode
    agent_name: str | None = None
    appended_system_prompt: str | None = None

    # Claude only
    agents_payload: str | None = None
    prompt_file_path: str | None = None

    # Claude + Codex
    user_turn_content: str | None = None

    # Codex only
    report_output_path: str | None = None
    base_instructions: str | None = None
    developer_instructions: str | None = None

    # OpenCode only
    skills: tuple[str, ...] = ()
    # Using Any for reference_items to avoid circular import with ReferenceItem.
    reference_items: tuple[Any, ...] = ()

    @model_validator(mode="after")
    def _validate_continue_fork_requires_session(self) -> ResolvedLaunchSpec:
        if self.continue_fork and not self.continue_session_id:
            raise ValueError("continue_fork=True requires continue_session_id")
        return self


@dataclass(frozen=True)
class ResolvedLaunchRouting:
    """Typed launch-routing output from policy resolution."""

    model: str | None
    harness: HarnessId
    agent: str | None

    def as_overrides(self) -> RuntimeOverrides:
        """Compatibility view for legacy RuntimeOverrides consumers."""

        return RuntimeOverrides(
            model=self.model,
            harness=self.harness.value,
            agent=self.agent,
        )


@dataclass(frozen=True)
class PreparedPromptPayload:
    """Typed prompt-channel payload carried from content composition into bind."""

    adhoc_agent_payload: str = ""
    appended_system_prompt: str | None = None
    user_turn_content: str | None = None


@dataclass(frozen=True)
class PreparedLaunchContent:
    """Typed content-composition output consumed by later launch binding."""

    final_prompt: str = ""
    resolved_context_from: tuple[str, ...] = ()
    loaded_references: tuple[ReferenceItem, ...] = ()
    prompt_payload: PreparedPromptPayload = field(default_factory=PreparedPromptPayload)
    projected_content: ProjectedContent | None = None
    agent_inventory_prompt: str | None = None
    context_prompt: str | None = None


@dataclass(frozen=True)
class PreparedLaunchRuntimeSeeds:
    """Prepare-phase runtime/session facts carried into bind."""

    seed_harness_session_id: str | None = None
    seed_session_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedLaunchEnvironment:
    """Categorized child-environment facts owned by bind/runtime resolution."""

    child_context_env: MappingProxyType[str, str]
    plan_env: MappingProxyType[str, str]
    preflight_env: MappingProxyType[str, str]
    workspace_env: MappingProxyType[str, str]
    runtime_override_env: MappingProxyType[str, str]
    bind_env_overrides: MappingProxyType[str, str]
    runner_overlay_env: MappingProxyType[str, str]
    final_env: MappingProxyType[str, str]

    @classmethod
    def build(
        cls,
        *,
        child_context_env: dict[str, str],
        plan_env: dict[str, str],
        preflight_env: dict[str, str],
        workspace_env: dict[str, str],
        runtime_override_env: dict[str, str],
        bind_env_overrides: dict[str, str],
        runner_overlay_env: dict[str, str],
        final_env: dict[str, str],
    ) -> ResolvedLaunchEnvironment:
        return cls(
            child_context_env=MappingProxyType(dict(child_context_env)),
            plan_env=MappingProxyType(dict(plan_env)),
            preflight_env=MappingProxyType(dict(preflight_env)),
            workspace_env=MappingProxyType(dict(workspace_env)),
            runtime_override_env=MappingProxyType(dict(runtime_override_env)),
            bind_env_overrides=MappingProxyType(dict(bind_env_overrides)),
            runner_overlay_env=MappingProxyType(dict(runner_overlay_env)),
            final_env=MappingProxyType(dict(final_env)),
        )


@dataclass(frozen=True)
class ResolvedLaunchBinding:
    """Typed runtime/session/launch-spec output produced by bind."""

    work_id: str | None
    child_cwd: Path
    report_output_path: Path
    run_params: ResolvedRunInputs
    permission_config: PermissionConfig
    perms: PermissionResolver
    spec: ResolvedLaunchSpec
    argv: tuple[str, ...]
    environment: ResolvedLaunchEnvironment
    effective_harness_session_id: str | None = None
    seed_harness_session_args: tuple[str, ...] = ()


SpecT = TypeVar("SpecT", bound=ResolvedLaunchSpec)


def summarize_composition_warnings(
    warnings: tuple[CompositionWarning, ...],
) -> str | None:
    parts = [warning.message.strip() for warning in warnings if warning.message.strip()]
    if not parts:
        return None
    return "; ".join(parts)


@dataclass(frozen=True)
class PreflightResult:
    """Result of adapter-owned preflight."""

    expanded_passthrough_args: tuple[str, ...]
    extra_env: MappingProxyType[str, str]

    @classmethod
    def build(
        cls,
        *,
        expanded_passthrough_args: tuple[str, ...],
        extra_env: dict[str, str] | None = None,
    ) -> PreflightResult:
        return cls(
            expanded_passthrough_args=expanded_passthrough_args,
            extra_env=MappingProxyType(dict(extra_env or {})),
        )
