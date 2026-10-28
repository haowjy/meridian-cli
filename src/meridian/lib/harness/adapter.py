"""Harness adapter contracts and shared data models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Generic,
    Literal,
    Protocol,
    TypeVar,
    final,
    runtime_checkable,
)

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.config.harness_models import PiHarnessProfileConfig
from meridian.lib.core.native_identity import (
    LaunchIntent,
    NativeIdentity,
    NativeKey,
    NativeKeyFields,
    NativeObservationBatch,
    NativeSessionUnavailable,
    Operation,
    PostExit,
)
from meridian.lib.core.overrides import AutocompactPctValue, AutocompactValue
from meridian.lib.core.types import HarnessId, ModelId, SpawnId, TransportId
from meridian.lib.harness.connections.base import (
    PrimaryRuntimeEventSurface,
    PrimaryRuntimeRequestPolicy,
    RawHarnessEvent,
    ServerRequestHandler,
)
from meridian.lib.harness.launch_types import SessionSeed
from meridian.lib.launch.composition import (
    ComposedLaunchContent,
    ProjectedContent,
    project_inline_content,
)
from meridian.lib.launch.launch_types import (
    CompositionWarning,
    ResolvedLaunchSpec,
    SpecT,
    TerminalSurfaceMode,
)
from meridian.lib.launch.request import SessionRequest
from meridian.lib.safety.policy import NetworkPolicy

if TYPE_CHECKING:
    from meridian.lib.harness.extractors.base import AttemptFold

AdapterSpecT = TypeVar("AdapterSpecT", bound=ResolvedLaunchSpec, covariant=True)


def _empty_metadata() -> dict[str, object]:
    return {}


def _empty_env_overrides() -> dict[str, str]:
    return {}


class BootstrapMode(StrEnum):
    """How one harness boots or materializes interactive primary runs."""

    SUBPROCESS_ONLY = "subprocess_only"
    MANAGED_PRIMARY_ATTACH = "managed_primary_attach"


class ForkMaterializationMode(StrEnum):
    """How continue-fork requests are materialized for one harness."""

    NATIVE_CONTINUE_FORK = "native_continue_fork"
    MERIDIAN_MATERIALIZED_FORK = "meridian_materialized_fork"


class HarnessCapabilities(BaseModel):
    """Feature flags for one harness implementation."""

    model_config = ConfigDict(frozen=True)

    supports_stdin_prompt: bool = False
    supports_session_resume: bool = False
    supports_session_fork: bool = False
    supports_native_agents: bool = False
    supports_primary_launch: bool = False
    supports_named_primary_resume: bool = False
    # Whether primary launch needs a synthetic first user prompt to bootstrap
    # one session. Harnesses that can attach without a first-turn prompt keep
    # this disabled.
    requires_initial_prompt: bool = False

    # Whether native file injection is available (e.g., OpenCode --file)
    supports_native_file_injection: bool = False
    # Whether a black-box primary launches with captured output in its
    # non-interactive print mode (Claude `--print`).
    captures_blackbox_output: bool = False
    terminal_surface_mode: TerminalSurfaceMode = TerminalSurfaceMode.PTY_MEDIATED


class HarnessContract(BaseModel):
    """Inspectable contract surface for one harness adapter."""

    model_config = ConfigDict(frozen=True)

    capabilities: HarnessCapabilities
    transport_ids: tuple[TransportId, ...]
    launch_spec_cls: str
    bootstrap_mode: BootstrapMode
    fork_materialization: ForkMaterializationMode = ForkMaterializationMode.NATIVE_CONTINUE_FORK
    primary_attach_failure_policy: Literal["raise", "fallback_to_blackbox"] = "raise"
    #: Whether the black-box primary child needs the stderr log path env var
    #: injected so its runtime writes stderr to the spawn dir.
    primary_stderr_log: bool = False
    primary_session_runtime_request_policy: PrimaryRuntimeRequestPolicy = (
        PrimaryRuntimeRequestPolicy.NONE
    )
    primary_session_runtime_event_surface: PrimaryRuntimeEventSurface = (
        PrimaryRuntimeEventSurface.NONE
    )


class SpawnUsageContractVariants(BaseModel):
    """Harness-specific phrasing for the shared spawn-usage contract template."""

    model_config = ConfigDict(frozen=True)

    intro_line: str
    double_wrap_bullet: str
    timeout_bullet: str


GENERIC_SPAWN_USAGE_VARIANTS = SpawnUsageContractVariants(
    intro_line="Launch detached, then wait — never block the turn or double-background.",
    double_wrap_bullet=(
        "NEVER wrap `meridian spawn --bg` in your harness's background execution.\n"
        "  It already detaches — double-wrapping adds nothing and lets the harness kill\n"
        "  the launcher mid-startup, before it hands off to the detached worker;\n"
        "  the spawn then fails (recorded and visible, but the work never runs)."
    ),
    timeout_bullet=(
        "NEVER block-foreground a long spawn (omitting --bg). It will outlive your\n"
        "  command timeout; you lose the thread while the spawn runs on."
    ),
)

CLAUDE_SPAWN_USAGE_VARIANTS = SpawnUsageContractVariants(
    intro_line="Launch detached, then wait — never block the turn or background-wrap.",
    double_wrap_bullet=(
        "NEVER wrap `meridian spawn --bg` inside Bash's run_in_background. It\n"
        "  already detaches — double-wrapping adds nothing and lets the harness kill\n"
        "  the launcher mid-startup, before it hands off to the detached worker; the\n"
        "  spawn then fails (recorded and visible, but the work never runs)."
    ),
    timeout_bullet=(
        "NEVER block-foreground a long spawn (omitting --bg). It will outlive your\n"
        "  Bash command timeout; you lose the thread while the spawn runs on."
    ),
)


class RunPromptPolicy(BaseModel):
    """Adapter-owned policy for composing one run prompt."""

    model_config = ConfigDict(frozen=True)

    spawn_usage_contract_variants: SpawnUsageContractVariants = GENERIC_SPAWN_USAGE_VARIANTS


class SpawnParams(BaseModel):
    """Inputs required to launch one harness run."""

    model_config = ConfigDict(frozen=True)

    prompt: str
    model: ModelId | None = None
    model_override_explicit: bool = False
    effort: str | None = None
    autocompact: AutocompactValue = None
    autocompact_pct: AutocompactPctValue = None
    skills: tuple[str, ...] = ()
    agent: str | None = None
    # Pre-built ad-hoc native-agent payload. Empty string when not used.
    adhoc_agent_payload: str = ""
    extra_args: tuple[str, ...] = ()
    control_root: str | None = None
    task_cwd: str | None = None
    projected_roots: tuple[Path, ...] = ()
    interactive: bool = False
    continue_harness_session_id: str | None = None
    continue_fork: bool = False
    appended_system_prompt: str | None = None
    user_turn_content: str | None = None
    reference_items: tuple[Any, ...] = ()
    pi_harness_profile: PiHarnessProfileConfig | None = None


class StreamEvent(BaseModel):
    """Open subprocess-stream observation parsed from unpinned harness output.

    This legacy execution-path envelope deliberately accepts unknown event names.
    Connection events use ``RawHarnessEvent`` and per-bundle semantic normalization.
    """

    model_config = ConfigDict(frozen=True)

    event_type: str
    category: str
    raw_line: str
    text: str | None = None
    metadata: dict[str, object] = Field(default_factory=_empty_metadata)


def _empty_prelaunch_metadata() -> dict[str, str]:
    return {}


class HarnessPrelaunchState(BaseModel):
    """Adapter-owned prelaunch result consumed by launch orchestrators."""

    model_config = ConfigDict(frozen=True)

    env_overrides: dict[str, str] = Field(default_factory=_empty_env_overrides)
    metadata: dict[str, str] = Field(default_factory=_empty_prelaunch_metadata)


class NativePrimaryRuntimeMetadata(BaseModel):
    """Harness-specific runtime fields projected into primary_meta.json."""

    model_config = ConfigDict(frozen=True)

    runtime_kind: str | None = None
    runtime_path: str | None = None
    runtime_version: str | None = None
    session_dir: str | None = None
    auth_policy: str | None = None


RecordConfigDirFn = Callable[[str], None]


@runtime_checkable
class SpawnExtractor(Protocol):
    """Attempt-local extraction interface for spawn finalization."""

    def create_fold(self) -> AttemptFold: ...

    def detect_session_id_from_event(self, event: RawHarnessEvent) -> str | None: ...

    def read_native_turn(self, key: NativeKey, turn_ids: tuple[str, ...]) -> str | None: ...


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

    def plan_native_identity(
        self, run: SpawnParams, *, preforked_session_id: str | None = None
    ) -> LaunchIntent | None: ...

    def native_store_for_launch(
        self,
        *,
        child_env: Mapping[str, str],
        child_cwd: Path,
        spawn_id: SpawnId,
        operation: Operation,
        interactive: bool,
    ) -> str: ...

    def finalize_native_identity(
        self,
        intent: LaunchIntent,
        *,
        child_env: dict[str, str],
        child_cwd: Path,
        session: SessionRequest,
        spawn_id: SpawnId,
        interactive: bool,
    ) -> NativeIdentity: ...

    def observe_after_exit(
        self,
        identity: NativeIdentity,
        entry: NativeKeyFields,
        *,
        child_env: Mapping[str, str],
        child_cwd: Path,
        pid: int | None,
        started_at_epoch: float | None,
    ) -> PostExit: ...

    def native_observation_reader(
        self,
        *,
        child_env: Mapping[str, str],
        pid: int,
    ) -> Callable[[int], NativeObservationBatch] | None: ...

    def resolve_launch_spec(self, run: SpawnParams) -> AdapterSpecT: ...

    def launch_spec_warnings(self, spec: ResolvedLaunchSpec) -> tuple[CompositionWarning, ...]: ...

    def launch_environment_warnings(
        self,
        *,
        child_env: Mapping[str, str],
        child_cwd: Path,
    ) -> tuple[CompositionWarning, ...]: ...


@runtime_checkable
class SubprocessHarness(HarnessAdapter[ResolvedLaunchSpec], Protocol):
    """Protocol for subprocess-launching harness behavior."""

    @property
    def capabilities(self) -> HarnessCapabilities: ...

    def run_prompt_policy(self) -> RunPromptPolicy: ...

    def shell_network_guidance(
        self,
        *,
        sandbox: str | None,
        network: NetworkPolicy,
        native_config: Mapping[str, object],
    ) -> str | None: ...

    def reject_removed_native_config(self, native_config: Mapping[str, object]) -> None: ...

    def build_adhoc_agent_payload(self, *, name: str, description: str, prompt: str) -> str: ...

    def env_defaults(self, *, run: SpawnParams, env: Mapping[str, str]) -> dict[str, str]: ...

    def project_launch_env(
        self,
        *,
        spec: ResolvedLaunchSpec,
        system_prompt_path: Path,
        env: Mapping[str, str],
    ) -> dict[str, str]: ...

    def blocked_child_env_vars(self) -> frozenset[str]: ...

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

    def resolve_primary_command(
        self,
        command: tuple[str, ...],
        *,
        state: HarnessPrelaunchState,
    ) -> tuple[str, ...]:
        """Project the resolved argv for a native primary launch (e.g. runtime path)."""
        ...

    def redact_primary_command(self, command: tuple[str, ...]) -> tuple[str, ...]:
        """Redact harness-specific secrets from an argv persisted to metadata."""
        ...

    def uses_native_primary_metadata(self) -> bool:
        """Return whether this harness writes native/black-box primary metadata."""
        ...

    def native_primary_runtime_metadata(
        self,
        state: HarnessPrelaunchState,
    ) -> NativePrimaryRuntimeMetadata:
        """Project prelaunch state into primary_meta.json runtime fields."""
        ...

    def build_primary_runtime_request_handler(
        self,
        *,
        spawn_dir: Path,
        event_sink: Callable[[RawHarnessEvent], Awaitable[None]],
    ) -> ServerRequestHandler | None:
        """Build a managed-primary runtime request handler for this harness."""
        ...

    def native_transcript_kind(self, path: Path) -> Literal["native_file", "opencode_db"]: ...

    def resolve_native_session_file(
        self,
        *,
        session_id: str,
        native_store: Path,
    ) -> Path | None: ...

    def resolve_session_file(
        self,
        *,
        project_root: Path,
        session_id: str,
        config_root_hint: Path | None = None,
    ) -> Path | None: ...

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

    def fork_session(self, source_session_id: str, *, native_store: str | None = None) -> str: ...

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

    native_identity: ClassVar[bool] = False
    refused_identity_flags: ClassVar[frozenset[str]] = frozenset()
    continues_in_source_store: ClassVar[frozenset[Operation]] = frozenset()
    resolves_untracked_source: ClassVar[bool] = False

    @final
    def plan_native_identity(
        self,
        run: SpawnParams,
        *,
        preforked_session_id: str | None = None,
    ) -> LaunchIntent | None:
        if not self.native_identity:
            return None
        for token in run.extra_args:
            if token.split("=", 1)[0] in self.refused_identity_flags:
                raise ValueError(
                    f"{self.id} managed identity refuses {token} in passthrough extra_args"
                )
        source = (run.continue_harness_session_id or "").strip() or None
        if preforked_session_id:
            intent = LaunchIntent("fork", preforked_session_id=preforked_session_id)
        else:
            intent = LaunchIntent(
                "fork" if source and run.continue_fork else "resume" if source else "create", source
            )
        self.validate_intent(intent)
        return intent

    @final
    def finalize_native_identity(
        self,
        intent: LaunchIntent,
        *,
        child_env: dict[str, str],
        child_cwd: Path,
        session: SessionRequest,
        spawn_id: SpawnId,
        interactive: bool,
    ) -> NativeIdentity:
        op, ref = intent.operation, session.source_ref
        source: Path | None = None
        if op != "create":
            wanted = intent.source_session_id or intent.preforked_session_id
            source_store = session.source_native_store
            if source_store is None and session.continue_source_tracked:
                raise NativeSessionUnavailable(ref, "unbound")
            if source_store is not None and op in self.continues_in_source_store:
                self.pin_native_store(child_env, source_store)
                if self._store(child_env, child_cwd, spawn_id, op, interactive) != source_store:
                    raise NativeSessionUnavailable(ref, "missing")
            if source_store is not None or self.resolves_untracked_source:
                lookup = source_store or self._store(
                    child_env, child_cwd, spawn_id, "resume", interactive
                )
                source = self._require_source(wanted, Path(lookup), ref=ref)
        store = self._store(child_env, child_cwd, spawn_id, op, interactive)
        self.pin_native_store(child_env, store)
        return NativeIdentity(
            str(self.id),
            op,
            store,
            self.assign_session_id(intent, store=Path(store)),
            intent.source_session_id,
            source,
        )

    def _store(
        self,
        env: Mapping[str, str],
        cwd: Path,
        spawn_id: SpawnId,
        operation: Operation,
        interactive: bool,
    ) -> str:
        return self.native_store_for_launch(
            child_env=env,
            child_cwd=cwd,
            spawn_id=spawn_id,
            operation=operation,
            interactive=interactive,
        )

    def _require_source(self, session_id: str | None, store: Path, *, ref: str) -> Path:
        if session_id is None:
            raise NativeSessionUnavailable(ref, "unbound")
        try:
            source = self.resolve_native_session_file(session_id=session_id, native_store=store)
        except NativeSessionUnavailable as exc:
            raise exc.for_ref(ref) from exc
        if source is None:
            raise NativeSessionUnavailable(ref, "missing")
        return source

    def validate_intent(self, intent: LaunchIntent) -> None:
        pass

    def native_store_for_launch(
        self,
        *,
        child_env: Mapping[str, str],
        child_cwd: Path,
        spawn_id: SpawnId,
        operation: Operation,
        interactive: bool,
    ) -> str:
        raise NotImplementedError

    def pin_native_store(self, child_env: dict[str, str], store: str) -> None:
        pass

    def assign_session_id(self, intent: LaunchIntent, *, store: Path) -> str | None:
        return (
            intent.source_session_id
            if intent.operation == "resume"
            else intent.preforked_session_id
        )

    def observe_after_exit(
        self,
        identity: NativeIdentity,
        entry: NativeKeyFields,
        *,
        child_env: Mapping[str, str],
        child_cwd: Path,
        pid: int | None,
        started_at_epoch: float | None,
    ) -> PostExit:
        return PostExit()

    def native_observation_reader(
        self,
        *,
        child_env: Mapping[str, str],
        pid: int,
    ) -> Callable[[int], NativeObservationBatch] | None:
        """Capture a launch-owned reader, or opt out without starting a drainer."""
        return None

    @abstractmethod
    def resolve_launch_spec(self, run: SpawnParams) -> SpecT:
        """Resolve typed launch spec from generic spawn parameters."""
        ...

    def launch_spec_warnings(self, spec: ResolvedLaunchSpec) -> tuple[CompositionWarning, ...]:
        """Return non-fatal diagnostics produced while resolving a launch spec."""

        _ = spec
        return ()

    def launch_environment_warnings(
        self,
        *,
        child_env: Mapping[str, str],
        child_cwd: Path,
    ) -> tuple[CompositionWarning, ...]:
        """Return diagnostics selected by the final child environment and cwd."""

        _ = child_env, child_cwd
        return ()

    def reject_removed_native_config(self, native_config: Mapping[str, object]) -> None:
        """Fail a fresh launch on native keys whose deprecation window has closed."""

        _ = native_config

    def run_prompt_policy(self) -> RunPromptPolicy:
        return RunPromptPolicy()

    def shell_network_guidance(
        self,
        *,
        sandbox: str | None,
        network: NetworkPolicy,
        native_config: Mapping[str, object],
    ) -> str | None:
        _ = sandbox, network, native_config
        return None

    def build_adhoc_agent_payload(self, *, name: str, description: str, prompt: str) -> str:
        _ = name, description, prompt
        return ""

    def fork_session(self, source_session_id: str, *, native_store: str | None = None) -> str:
        """Fork one harness session and return the new session ID."""

        _ = source_session_id
        raise NotImplementedError

    def owns_untracked_session(self, *, project_root: Path, session_ref: str) -> bool:
        _ = project_root, session_ref
        return False

    def blocked_child_env_vars(self) -> frozenset[str]:
        return frozenset()

    def env_defaults(self, *, run: SpawnParams, env: Mapping[str, str]) -> dict[str, str]:
        """Optional process defaults; explicit inherited or launch values win."""
        _ = run, env
        return {}

    def project_launch_env(
        self,
        *,
        spec: ResolvedLaunchSpec,
        system_prompt_path: Path,
        env: Mapping[str, str],
    ) -> dict[str, str]:
        """Env overrides that deliver the bound prompt, model, roots and permissions.

        ``system_prompt_path`` is where the appended prompt is written, after bind and
        before launch, whenever ``spec.appended_system_prompt`` is non-empty; ``env`` is
        the child env as bound.
        """
        _ = spec, system_prompt_path, env
        return {}

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

    def resolve_primary_command(
        self,
        command: tuple[str, ...],
        *,
        state: HarnessPrelaunchState,
    ) -> tuple[str, ...]:
        _ = state
        return command

    def redact_primary_command(self, command: tuple[str, ...]) -> tuple[str, ...]:
        return command

    def uses_native_primary_metadata(self) -> bool:
        return False

    def native_primary_runtime_metadata(
        self,
        state: HarnessPrelaunchState,
    ) -> NativePrimaryRuntimeMetadata:
        _ = state
        return NativePrimaryRuntimeMetadata()

    def build_primary_runtime_request_handler(
        self,
        *,
        spawn_dir: Path,
        event_sink: Callable[[RawHarnessEvent], Awaitable[None]],
    ) -> ServerRequestHandler | None:
        _ = spawn_dir, event_sink
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

    def native_transcript_kind(self, path: Path) -> Literal["native_file", "opencode_db"]:
        return "native_file"

    def resolve_native_session_file(
        self,
        *,
        session_id: str,
        native_store: Path,
    ) -> Path | None:
        return None

    def resolve_session_file(
        self,
        *,
        project_root: Path,
        session_id: str,
        config_root_hint: Path | None = None,
    ) -> Path | None:
        _ = project_root, session_id, config_root_hint
        return None
