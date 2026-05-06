"""Pure-data compiler contract for launch parameter resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from meridian.lib.catalog.agent import AgentModelEntry, FanoutEntry, ModelPolicyRule
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.config.settings import AgentOverlayConfig
from meridian.lib.core.overrides import RuntimeOverrides


class ProvenanceLevel(Enum):
    CLI = "cli"
    ENV = "env"
    AGENT_OVERLAY_POLICY = "agent-overlay-model-policy"
    AGENT_OVERLAY_DEFAULT = "agent-overlay-default"
    PROFILE_MODEL_POLICY = "profile-model-policy"
    PROFILE_DEFAULT = "profile-default"
    CONFIG_DEFAULT = "config-default"
    ALIAS_DEFAULT = "alias-default"
    HARNESS_FALLBACK = "harness-fallback"
    UNSET = "unset"


@dataclass(frozen=True)
class FieldProvenance:
    """Records where each effective field value came from."""

    model_source: ProvenanceLevel = ProvenanceLevel.UNSET
    harness_source: ProvenanceLevel = ProvenanceLevel.UNSET
    effort_source: ProvenanceLevel = ProvenanceLevel.UNSET
    approval_source: ProvenanceLevel = ProvenanceLevel.UNSET
    sandbox_source: ProvenanceLevel = ProvenanceLevel.UNSET
    autocompact_source: ProvenanceLevel = ProvenanceLevel.UNSET
    timeout_source: ProvenanceLevel = ProvenanceLevel.UNSET


@dataclass(frozen=True)
class CompilerRequest:
    """Pure-data input to the compiler. JSON-serializable."""

    # Identity
    requested_agent: str | None
    requested_model: str | None

    # Layered override sources — separate for provenance accuracy
    cli_overrides: RuntimeOverrides
    env_overrides: RuntimeOverrides
    agent_overlay: AgentOverlayConfig | None
    config_defaults: RuntimeOverrides

    # Agent profile data (loaded by caller, passed as data)
    profile_routing_model: str | None
    profile_routing_harness: str | None
    profile_policy_effort: str | None
    profile_policy_approval: str | None
    profile_policy_sandbox: str | None
    profile_policy_autocompact: int | None
    profile_model_policies: tuple[ModelPolicyRule, ...] | None  # None = no profile
    profile_legacy_models: dict[str, AgentModelEntry] | None
    profile_fanout: tuple[FanoutEntry, ...] | None
    profile_skills: tuple[str, ...]

    # Model catalog data
    resolved_alias_entry: AliasEntry | None
    alias_catalog: dict[str, AliasEntry]

    # Options
    configured_default_harness: str = "claude"
    project_root: str = ""


@dataclass(frozen=True)
class CompilerResult:
    """Pure-data compiler output. JSON-serializable."""

    # Identity
    agent_name: str | None
    profile_found: bool

    # Model + routing
    model: str
    model_token: str
    harness: str

    # Runtime policy
    effort: str | None
    approval: str | None
    sandbox: str | None
    autocompact: int | None
    timeout: float | None

    # Profile-derived content identifiers
    skill_names: tuple[str, ...]
    tools: tuple[str, ...] | None = None
    disallowed_tools: tuple[str, ...] | None = None
    mcp_tools: tuple[str, ...] | None = None

    # Selection context
    model_selection_requested_token: str = ""
    model_selection_canonical_id: str = ""
    harness_provenance: str = ""

    # Provenance + diagnostics
    field_provenance: FieldProvenance = field(default_factory=FieldProvenance)
    warnings: tuple[str, ...] = ()

    # Fallback info
    fallback_applied: bool = False
    fallback_model: str | None = None


def compile_launch_params(request: CompilerRequest) -> CompilerResult:
    """Compile launch parameters from layered inputs. Implemented in Phase 3.2."""

    _ = request
    raise NotImplementedError("Compiler algorithm not yet implemented")


__all__ = [
    "CompilerRequest",
    "CompilerResult",
    "FieldProvenance",
    "ProvenanceLevel",
    "compile_launch_params",
]
