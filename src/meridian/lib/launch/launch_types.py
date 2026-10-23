"""Leaf launch contracts shared across launch policy, content, and runners."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from meridian.lib.core.execution_policy import (
    NativeConfigValue,
)
from meridian.lib.core.execution_policy import (
    ResolvedExecutionPolicy as ResolvedExecutionPolicy,
)
from meridian.lib.core.native_identity import NativeIdentity
from meridian.lib.core.overrides import AutocompactPctValue, AutocompactValue
from meridian.lib.safety.policy import PermissionPolicy

if TYPE_CHECKING:
    from meridian.lib.core.types import HarnessId
    from meridian.lib.harness.adapter import SpawnParams
    from meridian.lib.launch.composition import ComposedLaunchContent, ProjectedContent
    from meridian.lib.launch.reference import ReferenceItem


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


@dataclass(frozen=True)
class ClaudeRuntimeResolution:
    """Per-launch Claude facts shared by projection and permission reporting."""

    plugin_dirs: tuple[str, ...] = ()
    warnings: tuple[CompositionWarning, ...] = ()
    settings_allows: tuple[tuple[str, tuple[str, ...]], ...] = ()


class ResolvedLaunchSpec(BaseModel):
    """Transport-neutral resolved configuration for one harness launch."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # Identity
    # str at runtime (HarnessId is StrEnum — direct import triggers harness __init__ bootstrap).
    harness: str | None = None
    model: str | None = None

    # Execution parameters
    effort: str | None = None
    autocompact: AutocompactValue = None
    autocompact_pct: AutocompactPctValue = None
    prompt: str = ""

    # Session continuity
    continue_session_id: str | None = None
    continue_fork: bool = False
    native_identity: NativeIdentity | None = None

    # Permissions: defaults to no tool rules and default sandbox/approval.
    permission_policy: PermissionPolicy = PermissionPolicy()
    # Harness-native execution settings preserved from the selected Mars profile.
    native_config: NativeConfigValue = Field(
        default_factory=lambda: ResolvedExecutionPolicy().native_config
    )

    # Passthrough args forwarded verbatim to the harness process.
    extra_args: tuple[str, ...] = ()

    # Interactive mode
    interactive: bool = False

    # First-class workspace root projection — harness-agnostic, deduplicated.
    projected_roots: tuple[Path, ...] = ()

    # Adapter-local projections: fields used by specific harness adapters.
    # All are optional and default to their zero values so unused adapters
    # don't need to set them.

    # Harness-level agent identity. Codex also shows it in the managed bootstrap turn.
    agent_name: str | None = None
    appended_system_prompt: str | None = None

    # Claude only
    agents_payload: str | None = None
    prompt_file_path: str | None = None
    claude_runtime: ClaudeRuntimeResolution = ClaudeRuntimeResolution()

    # Claude + Codex
    user_turn_content: str | None = None

    # Codex only: subprocess `-o` target; None for every other harness.
    report_output_path: str | None = None
    base_instructions: str | None = None
    developer_instructions: str | None = None

    # OpenCode only
    skills: tuple[str, ...] = ()
    # Using Any for reference_items to avoid circular import with ReferenceItem.
    reference_items: tuple[Any, ...] = ()
    # OpenCode's backend version, resolved once at bind from ``[harness.opencode] version``
    # and the installed binary (the harness's ``bind_spec`` port). The permission compile,
    # the config projection, argv guards and the connection all read it. ``None`` on a
    # spec that did not pass through bind.
    opencode_version: Literal["v1", "v2"] | None = None

    task_cwd: str | None = None

    # Pi only
    pi_extension_entrypoints: tuple[str, ...] = ()
    load_all_pi_extensions: bool = False

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
    loaded_references: tuple[ReferenceItem, ...] = ()
    prompt_payload: PreparedPromptPayload = field(default_factory=PreparedPromptPayload)
    projected_content: ProjectedContent | None = None
    composed_content: ComposedLaunchContent | None = None


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
        runtime_override_env: dict[str, str],
        bind_env_overrides: dict[str, str],
        runner_overlay_env: dict[str, str],
        final_env: dict[str, str],
    ) -> ResolvedLaunchEnvironment:
        return cls(
            child_context_env=MappingProxyType(dict(child_context_env)),
            plan_env=MappingProxyType(dict(plan_env)),
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
    run_params: SpawnParams
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
