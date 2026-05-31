"""Harness adapter contracts and shared data models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, Generic, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.config.settings import PiHarnessProfileConfig
from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import ArtifactKey, HarnessId, ModelId, SpawnId, TransportId
from meridian.lib.harness.connections.base import (
    PrimaryRuntimeEventSurface,
    PrimaryRuntimeRequestPolicy,
)
from meridian.lib.harness.launch_types import SessionSeed
from meridian.lib.launch.composition import (
    ComposedLaunchContent,
    ProjectedContent,
    project_inline_content,
)
from meridian.lib.launch.launch_types import (
    PermissionResolver,
    PreflightResult,
    ResolvedLaunchSpec,
    SpecT,
    TerminalSurfaceMode,
)
from meridian.lib.launch.request import SessionRequest
from meridian.lib.safety.permissions import PermissionConfig

AdapterSpecT = TypeVar("AdapterSpecT", bound=ResolvedLaunchSpec, covariant=True)


def _empty_metadata() -> dict[str, object]:
    return {}


def _empty_env_overrides() -> dict[str, str]:
    return {}


class ProjectionMode(StrEnum):
    """How one adapter projects semantic launch content onto harness channels."""

    # Prompt is written to a temp prompt file and system instructions are
    # appended into that file content before launch.
    PROMPT_FILE_APPEND_SYSTEM = "prompt_file_append_system"
    # Harness accepts structured system/user fields separately.
    SYSTEM_FIELD_WITH_USER_TURN = "system_field_with_user_turn"
    # Prompt is projected as one positional CLI argument.
    POSITIONAL_PROMPT = "positional_prompt"


class RuntimeHitlMode(StrEnum):
    """How one harness exposes runtime approval/user-input requests."""

    NONE = "none"
    CONNECTION_REQUESTS = "connection_requests"


class BootstrapMode(StrEnum):
    """How one harness boots or materializes interactive primary runs."""

    SUBPROCESS_ONLY = "subprocess_only"
    MANAGED_PRIMARY_ATTACH = "managed_primary_attach"


class ForkMaterializationMode(StrEnum):
    """How continue-fork requests are materialized for one harness."""

    NATIVE_CONTINUE_FORK = "native_continue_fork"
    MERIDIAN_MATERIALIZED_FORK = "meridian_materialized_fork"


class SessionSeedMode(StrEnum):
    """How one harness derives a seeded session id before runtime events begin."""

    NONE = "none"
    PROJECTED_ARGS = "projected_args"


class PrelaunchBootstrapMode(StrEnum):
    """How one harness performs prelaunch environment/bootstrap work."""

    NONE = "none"
    ENV_OVERLAY_AND_SESSION_ACCESS = "env_overlay_and_session_access"


class HarnessCapabilities(BaseModel):
    """Feature flags for one harness implementation."""

    model_config = ConfigDict(frozen=True)

    supports_stream_events: bool = True
    supports_stdin_prompt: bool = False
    supports_session_resume: bool = False
    supports_session_fork: bool = False
    supports_native_skills: bool = False
    supports_native_agents: bool = False
    supports_primary_launch: bool = False
    # Whether primary launch needs a synthetic first user prompt to bootstrap
    # one session. Harnesses that can attach without a first-turn prompt keep
    # this disabled.
    requires_initial_prompt: bool = False

    # Whether native file injection is available (e.g., OpenCode --file)
    supports_native_file_injection: bool = False
    terminal_surface_modes: tuple[TerminalSurfaceMode, ...] = (TerminalSurfaceMode.PTY_MEDIATED,)
    default_terminal_surface_mode: TerminalSurfaceMode = TerminalSurfaceMode.PTY_MEDIATED


class TransportContract(BaseModel):
    """Explicit transport responsibility declaration for one harness."""

    model_config = ConfigDict(frozen=True)

    transport_ids: tuple[TransportId, ...]
    observer_controller_required: bool = False


class ProjectionContract(BaseModel):
    """Explicit projection responsibility declaration for one harness."""

    model_config = ConfigDict(frozen=True)

    launch_spec_cls: str
    mode: ProjectionMode
    owns_prompt_policy: bool = True
    owns_mcp_projection: bool = True
    owns_env_overrides: bool = True


class ExtractionContract(BaseModel):
    """Explicit extraction responsibility declaration for one harness."""

    model_config = ConfigDict(frozen=True)

    extracts_usage: bool = True
    extracts_session_id: bool = True
    extracts_report: bool = True
    session_observation_order: tuple[str, ...] = ()


class ApprovalContract(BaseModel):
    """Explicit approval/HITL contract for one harness."""

    model_config = ConfigDict(frozen=True)

    runtime_hitl: RuntimeHitlMode = RuntimeHitlMode.NONE
    subprocess_permission_flags_projected_by_shared_policy: bool = True
    default_runtime_request_policy: Literal["none", "auto_accept"] = "none"
    primary_session_runtime_request_policy: PrimaryRuntimeRequestPolicy = (
        PrimaryRuntimeRequestPolicy.NONE
    )
    primary_session_runtime_event_surface: PrimaryRuntimeEventSurface = (
        PrimaryRuntimeEventSurface.NONE
    )


class ObserverControllerContract(BaseModel):
    """Managed primary observer/controller backend contract."""

    model_config = ConfigDict(frozen=True)

    starts_sidecar_backend: bool = True
    exposes_ordered_event_stream: bool = True
    supports_cancel: bool = True
    supports_input_injection: bool = True
    obtains_session_id_during_startup: bool = True


class BootstrapContract(BaseModel):
    """Explicit bootstrap/materialization contract for one harness."""

    model_config = ConfigDict(frozen=True)

    mode: BootstrapMode
    fork_materialization: ForkMaterializationMode = ForkMaterializationMode.NATIVE_CONTINUE_FORK
    primary_attach_failure_policy: Literal["raise", "fallback_to_blackbox"] = "raise"
    seeds_resume_metadata: bool = True
    primary_session_seed_mode: SessionSeedMode = SessionSeedMode.NONE
    streaming_session_seed_mode: SessionSeedMode = SessionSeedMode.NONE
    prelaunch_bootstrap_mode: PrelaunchBootstrapMode = PrelaunchBootstrapMode.NONE
    observer_controller: ObserverControllerContract | None = None


class HarnessContract(BaseModel):
    """Inspectable contract surface for one harness adapter."""

    model_config = ConfigDict(frozen=True)

    capabilities: HarnessCapabilities
    transport: TransportContract
    projection: ProjectionContract
    extraction: ExtractionContract
    approval: ApprovalContract
    bootstrap: BootstrapContract
    capability_limits: tuple[str, ...] = ()


class RunPromptPolicy(BaseModel):
    """Adapter-owned policy for composing one run prompt."""

    model_config = ConfigDict(frozen=True)

    skill_injection_mode: Literal["none", "append-system-prompt"] = "none"


class SpawnParams(BaseModel):
    """Inputs required to launch one harness run."""

    model_config = ConfigDict(frozen=True)

    prompt: str
    model: ModelId | None = None
    effort: str | None = None
    skills: tuple[str, ...] = ()
    agent: str | None = None
    # Pre-built ad-hoc native-agent payload. Empty string when not used.
    adhoc_agent_payload: str = ""
    extra_args: tuple[str, ...] = ()
    control_root: str | None = None
    task_cwd: str | None = None
    mcp_tools: tuple[str, ...] = ()
    projected_roots: tuple[Path, ...] = ()
    interactive: bool = False
    continue_harness_session_id: str | None = None
    continue_fork: bool = False
    appended_system_prompt: str | None = None
    report_output_path: str | None = None
    user_turn_content: str | None = None
    context_from_payload: tuple[str, ...] = ()
    reference_items: tuple[Any, ...] = ()
    pi_harness_profile: PiHarnessProfileConfig | None = None


class McpConfig(BaseModel):
    """Harness-specific MCP wiring details for one run."""

    model_config = ConfigDict(frozen=True)

    command_args: tuple[str, ...] = ()
    env_overrides: dict[str, str] = Field(default_factory=_empty_env_overrides)
    claude_allowed_tools: tuple[str, ...] = ()


class StreamEvent(BaseModel):
    """Structured stream event parsed from harness output."""

    model_config = ConfigDict(frozen=True)

    event_type: str
    category: str
    raw_line: str
    text: str | None = None
    metadata: dict[str, object] = Field(default_factory=_empty_metadata)


class SpawnResult(BaseModel):
    """Result payload for one completed execution."""

    model_config = ConfigDict(frozen=True)

    status: str
    output: str
    usage: TokenUsage = Field(default_factory=TokenUsage)
    harness_session_id: str | None = None
    raw_response: dict[str, object] | None = None


def _empty_prelaunch_metadata() -> dict[str, str]:
    return {}


class HarnessPrelaunchState(BaseModel):
    """Adapter-owned prelaunch result consumed by launch orchestrators."""

    model_config = ConfigDict(frozen=True)

    env_overrides: dict[str, str] = Field(default_factory=_empty_env_overrides)
    metadata: dict[str, str] = Field(default_factory=_empty_prelaunch_metadata)


RecordConfigDirFn = Callable[[str], None]


@runtime_checkable
class ArtifactStore(Protocol):
    """Artifact access used for usage/session extraction."""

    def get(self, key: ArtifactKey) -> bytes: ...

    def exists(self, key: ArtifactKey) -> bool: ...


@runtime_checkable
class SpawnExtractor(Protocol):
    """Artifact extraction interface for spawn finalization."""

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage: ...

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None: ...

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None: ...


@runtime_checkable
class HarnessAdapter(Protocol, Generic[AdapterSpecT]):
    """Typed harness adapter contract."""

    @property
    def id(self) -> HarnessId: ...

    @property
    def contract(self) -> HarnessContract: ...

    @property
    def consumed_fields(self) -> frozenset[str]: ...

    @property
    def explicitly_ignored_fields(self) -> frozenset[str]: ...

    @property
    def handled_fields(self) -> frozenset[str]: ...

    def resolve_launch_spec(self, run: SpawnParams, perms: PermissionResolver) -> AdapterSpecT: ...

    def preflight(
        self,
        *,
        execution_cwd: Path,
        child_cwd: Path,
        passthrough_args: tuple[str, ...],
    ) -> PreflightResult: ...


@runtime_checkable
class SubprocessHarness(HarnessAdapter[ResolvedLaunchSpec], Protocol):
    """Protocol for subprocess-launching harness behavior."""

    @property
    def capabilities(self) -> HarnessCapabilities: ...

    def run_prompt_policy(self) -> RunPromptPolicy: ...

    def build_adhoc_agent_payload(self, *, name: str, description: str, prompt: str) -> str: ...

    def build_command(self, run: SpawnParams, perms: PermissionResolver) -> list[str]: ...

    def mcp_config(self, run: SpawnParams) -> McpConfig | None: ...

    def env_overrides(self, config: PermissionConfig) -> dict[str, str]: ...

    def blocked_child_env_vars(self) -> frozenset[str]: ...

    def derive_primary_seeded_session_id(
        self,
        *,
        spec: ResolvedLaunchSpec,
        command: tuple[str, ...],
    ) -> str | None: ...

    def derive_streaming_seeded_session_id(
        self,
        *,
        spec: ResolvedLaunchSpec,
    ) -> str | None: ...

    def prepare_prelaunch(
        self,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        session: SessionRequest,
        child_cwd: Path,
        child_env: dict[str, str],
        resolved_harness_session_id: str,
        record_effective_config_dir: RecordConfigDirFn | None = None,
    ) -> HarnessPrelaunchState: ...

    def cleanup_prelaunch(
        self,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        chat_id: str | None,
        state: HarnessPrelaunchState,
    ) -> None: ...

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage: ...

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None: ...

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None: ...

    def resolve_session_file(self, *, project_root: Path, session_id: str) -> Path | None: ...

    def seed_session(
        self,
        *,
        is_resume: bool,
        harness_session_id: str,
        passthrough_args: tuple[str, ...],
    ) -> SessionSeed: ...

    def project_content(self, content: ComposedLaunchContent) -> ProjectedContent:
        """Project semantic content blocks to harness channel assignments.

        Takes harness-agnostic ComposedLaunchContent and returns
        ProjectedContent with harness-specific channel routing decisions.
        """
        ...

    def detect_primary_session_id(
        self,
        *,
        project_root: Path,
        started_at_epoch: float,
        started_at_local_iso: str | None,
        expected_session_id: str | None = None,
    ) -> str | None: ...

    def observe_session_id(
        self,
        *,
        artifacts: ArtifactStore,
        spawn_id: SpawnId | None = None,
        current_session_id: str | None = None,
        connection_session_id: str | None = None,
        project_root: Path | None = None,
        started_at_epoch: float | None = None,
        started_at_local_iso: str | None = None,
        expected_session_id: str | None = None,
    ) -> str | None:
        """Return the best available session ID observed after one execution.

        Priority order (each step falls through only if the result is empty):
        1. *connection_session_id* — live session id from the transport
           layer (e.g. HTTP/WS adapters that know the session id at
           connection time).
        2. Artifact extraction via ``extract_session_id()``.
        3. *current_session_id* — previously known id, returned as fallback
           so callers can treat the result as authoritative.
        4. Primary-session detection via ``detect_primary_session_id()``
           (only when *project_root* and *started_at_epoch* are supplied).

        I-4 contract: called exactly once per launch, by the driving adapter
        after the executor returns.  MUST NOT read or write adapter-instance
        singleton state shared across launches.
        """
        ...

    def fork_session(self, source_session_id: str) -> str: ...

    def owns_untracked_session(self, *, project_root: Path, session_ref: str) -> bool:
        """Return True if this harness owns the given untracked session reference."""
        ...


class BaseHarnessAdapter(Generic[SpecT], ABC):
    """Base adapter with contract enforcement and optional helper defaults."""

    @property
    @abstractmethod
    def id(self) -> HarnessId:
        """Harness identifier for this adapter."""
        ...

    @property
    @abstractmethod
    def contract(self) -> HarnessContract:
        """Explicit inspectable contract for this adapter."""
        ...

    @property
    @abstractmethod
    def consumed_fields(self) -> frozenset[str]:
        """SpawnParams fields actively consumed by this adapter."""
        ...

    @property
    @abstractmethod
    def explicitly_ignored_fields(self) -> frozenset[str]:
        """SpawnParams fields deliberately ignored by this adapter."""
        ...

    @property
    def handled_fields(self) -> frozenset[str]:
        return self.consumed_fields | self.explicitly_ignored_fields

    @abstractmethod
    def resolve_launch_spec(self, run: SpawnParams, perms: PermissionResolver) -> SpecT:
        """Resolve typed launch spec from generic spawn parameters."""
        ...

    def preflight(
        self,
        *,
        execution_cwd: Path,
        child_cwd: Path,
        passthrough_args: tuple[str, ...],
    ) -> PreflightResult:
        _ = execution_cwd, child_cwd
        return PreflightResult.build(expanded_passthrough_args=passthrough_args)

    def run_prompt_policy(self) -> RunPromptPolicy:
        return RunPromptPolicy()

    def build_adhoc_agent_payload(self, *, name: str, description: str, prompt: str) -> str:
        _ = name, description, prompt
        return ""

    def fork_session(self, source_session_id: str) -> str:
        """Fork one harness session and return the new session ID."""

        _ = source_session_id
        raise NotImplementedError

    def owns_untracked_session(self, *, project_root: Path, session_ref: str) -> bool:
        _ = project_root, session_ref
        return False

    def blocked_child_env_vars(self) -> frozenset[str]:
        return frozenset()

    def derive_primary_seeded_session_id(
        self,
        *,
        spec: ResolvedLaunchSpec,
        command: tuple[str, ...],
    ) -> str | None:
        _ = spec, command
        return None

    def derive_streaming_seeded_session_id(
        self,
        *,
        spec: ResolvedLaunchSpec,
    ) -> str | None:
        _ = spec
        return None

    def prepare_prelaunch(
        self,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        session: SessionRequest,
        child_cwd: Path,
        child_env: dict[str, str],
        resolved_harness_session_id: str,
        record_effective_config_dir: RecordConfigDirFn | None = None,
    ) -> HarnessPrelaunchState:
        _ = (
            runtime_root,
            spawn_id,
            session,
            child_cwd,
            child_env,
            resolved_harness_session_id,
            record_effective_config_dir,
        )
        return HarnessPrelaunchState()

    def cleanup_prelaunch(
        self,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        chat_id: str | None,
        state: HarnessPrelaunchState,
    ) -> None:
        _ = runtime_root, spawn_id, chat_id, state
        return None

    def seed_session(
        self,
        *,
        is_resume: bool,
        harness_session_id: str,
        passthrough_args: tuple[str, ...],
    ) -> SessionSeed:
        _ = is_resume, harness_session_id, passthrough_args
        return SessionSeed()

    def project_content(self, content: ComposedLaunchContent) -> ProjectedContent:
        """Default projection: all SYSTEM_INSTRUCTION inline at top, then context/user.

        Concrete adapters (Claude, Codex, OpenCode) should override for
        harness-specific channel routing.
        """
        return project_inline_content(content)

    def detect_primary_session_id(
        self,
        *,
        project_root: Path,
        started_at_epoch: float,
        started_at_local_iso: str | None,
        expected_session_id: str | None = None,
    ) -> str | None:
        _ = project_root, started_at_epoch, started_at_local_iso, expected_session_id
        return None

    def observe_session_id(
        self,
        *,
        artifacts: ArtifactStore,
        spawn_id: SpawnId | None = None,
        current_session_id: str | None = None,
        connection_session_id: str | None = None,
        project_root: Path | None = None,
        started_at_epoch: float | None = None,
        started_at_local_iso: str | None = None,
        expected_session_id: str | None = None,
    ) -> str | None:
        """Return the best observed session ID after one execution.

        Default priority: connection_session_id > extract_session_id >
        current_session_id > detect_primary_session_id.

        Concrete adapters may override for harness-specific extraction.
        """

        def _norm(v: str | None) -> str | None:
            if not v:
                return None
            stripped = v.strip()
            return stripped or None

        live = _norm(connection_session_id)
        if live:
            return live

        if spawn_id is not None:
            extracted = _norm(self.extract_session_id(artifacts, spawn_id))
            if extracted:
                return extracted

        current = _norm(current_session_id)
        if current:
            return current

        if project_root is not None and started_at_epoch is not None:
            detected = _norm(
                self.detect_primary_session_id(
                    project_root=project_root,
                    started_at_epoch=started_at_epoch,
                    started_at_local_iso=started_at_local_iso,
                    expected_session_id=expected_session_id,
                )
            )
            if detected:
                return detected

        return None

    def mcp_config(self, run: SpawnParams) -> McpConfig | None:
        _ = run
        return None

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        """Return the harness session ID from spawn artifacts, if available.

        Default returns None; concrete adapters that support session extraction override this.
        """

        _ = artifacts, spawn_id
        return None

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        _ = artifacts, spawn_id
        return None

    def resolve_session_file(self, *, project_root: Path, session_id: str) -> Path | None:
        _ = project_root, session_id
        return None
