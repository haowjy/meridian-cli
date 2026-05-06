"""Harness adapter contracts and shared data models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Generic, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import ArtifactKey, ModelId, SpawnId
from meridian.lib.harness.ids import HarnessId
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
)
from meridian.lib.safety.permissions import PermissionConfig

AdapterSpecT = TypeVar("AdapterSpecT", bound=ResolvedLaunchSpec, covariant=True)


def _empty_metadata() -> dict[str, object]:
    return {}


def _empty_env_overrides() -> dict[str, str]:
    return {}


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

    # Whether native file injection is available (e.g., OpenCode --file)
    supports_native_file_injection: bool = False


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
    project_root: str | None = None
    mcp_tools: tuple[str, ...] = ()
    projected_roots: tuple[Path, ...] = ()
    interactive: bool = False
    continue_harness_session_id: str | None = None
    continue_fork: bool = False
    appended_system_prompt: str | None = None
    report_output_path: str | None = None
    user_turn_content: str | None = None


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
    def consumed_fields(self) -> frozenset[str]: ...

    @property
    def explicitly_ignored_fields(self) -> frozenset[str]: ...

    @property
    def handled_fields(self) -> frozenset[str]: ...

    def resolve_launch_spec(
        self, run: SpawnParams, perms: PermissionResolver
    ) -> AdapterSpecT: ...

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
