"""Raw launch request DTOs persisted across prepare/execute boundaries."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.launch.composition import PromptDocument
from meridian.lib.launch.launch_types import TerminalSurfaceMode
from meridian.lib.tools import ToolsField


def _empty_template_vars() -> dict[str, str]:
    return {}


def _empty_agent_metadata() -> dict[str, str]:
    return {}


def _empty_config_snapshot() -> dict[str, object]:
    return {}


class RetryPolicy(BaseModel):
    """Retry configuration for spawn execution."""

    model_config = ConfigDict(frozen=True)

    max_attempts: int = 1
    backoff_secs: float = 2.0


class SessionRequest(BaseModel):
    """Session continuation options carried across prepare/execute boundaries."""

    model_config = ConfigDict(frozen=True)

    continue_chat_id: str | None = None
    requested_harness_session_id: str | None = None
    continue_fork: bool = False
    source_control_root: str | None = None
    source_execution_cwd: str | None = None
    source_claude_config_dir: str | None = None
    source_pi_session_dir: str | None = None
    forked_from_chat_id: str | None = None
    continue_harness: str | None = None
    continue_source_tracked: bool = False
    continue_source_ref: str | None = None
    primary_session_mode: str | None = None


def is_exact_continue_session(session: SessionRequest) -> bool:
    """True when a session request represents exact continuation, not fork/fresh."""

    primary_mode = ((session.primary_session_mode or "").strip().lower()) or None
    return (
        not session.continue_fork
        and (
            primary_mode == "resume"
            or (
                ((session.requested_harness_session_id or "").strip() != "")
                and ((session.continue_source_ref or "").strip() != "")
            )
        )
    )


class RequestPromptPayload(BaseModel):
    """Typed managed prompt payload carried across persisted request boundaries."""

    model_config = ConfigDict(frozen=True)

    adhoc_agent_payload: str = ""
    appended_system_prompt: str | None = None
    user_turn_content: str | None = None


class SpawnRequest(BaseModel):
    """Raw spawn request used to reconstruct launch composition at execute time."""

    model_config = ConfigDict(frozen=True)

    # Prompt / agent / skills
    prompt: str
    prompt_is_composed: bool = True
    primary_prompt_is_synthetic: bool = False
    model: str | None = None
    harness: str | None = None
    agent: str | None = None
    agent_opt_out: bool = False
    skills: tuple[str, ...] = ()
    supplemental_prompt_documents: tuple[PromptDocument, ...] = ()

    # Harness shape
    extra_args: tuple[str, ...] = ()
    mcp_tools: tuple[str, ...] = ()
    tools: ToolsField | None = None
    env: dict[str, str] = Field(default_factory=dict)

    # Execution policy carrier (replaces flat effort/sandbox/approval/autocompact/autocompact_pct)
    execution_policy: ResolvedExecutionPolicy = Field(default_factory=ResolvedExecutionPolicy)

    # Execution policy (nested)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)

    # Session intent (nested)
    session: SessionRequest = Field(default_factory=SessionRequest)

    # Context plumbing
    context_from: tuple[str, ...] = ()
    reference_files: tuple[str, ...] = ()
    template_vars: dict[str, str] = Field(default_factory=_empty_template_vars)

    # Routing & metadata
    work_id_hint: str | None = None
    inherited_context_work_id: str | None = None
    goal: str | None = None
    warning: str | None = None
    agent_metadata: dict[str, str] = Field(default_factory=_empty_agent_metadata)
    prompt_payload: RequestPromptPayload = Field(default_factory=RequestPromptPayload)
    launch_policy_snapshot: LaunchPolicySnapshot | None = None

    # Resolved metadata (computed at prepare time; NOT used by executors for composition)
    skill_paths: tuple[str, ...] = ()
    model_selection_requested_token: str | None = None
    model_selection_canonical_id: str | None = None
    model_selection_harness_provenance: str | None = None
    matched_policy_rule: str | None = None
    fallback_chain: tuple[dict[str, object], ...] = ()
    terminal_surface_mode: TerminalSurfaceMode | None = None
    # Preview command for dry-run display only.  Executors MUST NOT use this field.
    cli_command: tuple[str, ...] = ()
    authority_root: str | None = None
    task_cwd: str | None = None
    reference_anchor: str | None = None
    task_cwd_source: str | None = None
    task_cwd_work_item: str | None = None
    pi_task_ping_interval_seconds: float | None = None
    pi_task_ping_reset_on_activity: bool | None = None


class LaunchArgvIntent(StrEnum):
    """Whether the caller needs subprocess argv or only the typed launch spec."""

    REQUIRED = "required"
    SPEC_ONLY = "spec_only"


class LaunchCompositionSurface(StrEnum):
    """Which launch surface is asking the factory to normalize raw inputs."""

    DIRECT = "direct"
    PRIMARY = "primary"
    SPAWN_PREPARE = "spawn_prepare"


class LaunchRuntime(BaseModel):
    """Runtime context known by the driving adapter, not provided by callers."""

    model_config = ConfigDict(frozen=True)

    argv_intent: LaunchArgvIntent = LaunchArgvIntent.REQUIRED
    # DIRECT = pre-resolved request; spawn must use build_spawn_mars_runtime.
    composition_surface: LaunchCompositionSurface = LaunchCompositionSurface.DIRECT
    config_snapshot: dict[str, object] = Field(default_factory=_empty_config_snapshot)
    runtime_override_snapshot: dict[str, object] | None = None
    unsafe_no_permissions: bool = False
    debug: bool = False
    runtime_root: str
    config_root: str | None = None
    control_root: str | None = None
    requested_task_cwd: str | None = None
    # Legacy aliases retained for compatibility with older persisted payloads.
    # project_paths_project_root == config_root
    # project_paths_execution_cwd == requested_task_cwd
    project_paths_project_root: str | None = None
    project_paths_execution_cwd: str | None = None

    @property
    def resolved_config_root(self) -> str:
        configured = (self.config_root or "").strip()
        if configured:
            return configured
        legacy = (self.project_paths_project_root or "").strip()
        if legacy:
            return legacy
        raise ValueError("LaunchRuntime is missing config_root/project_paths_project_root.")

    @property
    def resolved_control_root(self) -> str:
        explicit = (self.control_root or "").strip()
        if explicit:
            return explicit
        return self.resolved_config_root

    @property
    def resolved_requested_task_cwd(self) -> str | None:
        explicit = (self.requested_task_cwd or "").strip()
        if explicit:
            return explicit
        legacy = (self.project_paths_execution_cwd or "").strip()
        return legacy or None

    @property
    def has_runtime_override_snapshot(self) -> bool:
        return self.runtime_override_snapshot is not None

    @property
    def resolved_runtime_overrides(self) -> RuntimeOverrides:
        if self.runtime_override_snapshot is not None:
            return RuntimeOverrides.model_validate(self.runtime_override_snapshot)
        return RuntimeOverrides()


__all__ = [
    "LaunchArgvIntent",
    "LaunchCompositionSurface",
    "LaunchPolicySnapshot",
    "LaunchRuntime",
    "RequestPromptPayload",
    "RetryPolicy",
    "SessionRequest",
    "SpawnRequest",
]
