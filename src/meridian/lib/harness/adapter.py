"""The adapter contract: the protocols an adapter implements and the types it returns.

Code outside ``lib/harness/`` reaches a harness through these and the registry; the shared
defaults adapters build on are in ``adapter_base``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
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
    runtime_checkable,
)

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from meridian.lib.config.harness_models import PiHarnessProfileConfig
from meridian.lib.core.native_identity import (
    LaunchIntent,
    NativeIdentity,
    NativeKey,
    NativeKeyFields,
    NativeObservationBatch,
    NativeSessionEvidence,
    NativeStoreMove,
    Operation,
    PostExit,
    PrecreatedSession,
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
from meridian.lib.harness.native_witness import Witness
from meridian.lib.launch.composition import (
    ComposedLaunchContent,
    ProjectedContent,
)
from meridian.lib.launch.launch_types import (
    CompositionWarning,
    ResolvedLaunchSpec,
    TerminalSurfaceMode,
)
from meridian.lib.launch.request import SessionRequest
from meridian.lib.safety.policy import NetworkPolicy
from meridian.lib.state.paths import LaunchPaths

if TYPE_CHECKING:
    from meridian.lib.harness.capture_qualify import CaptureObserver
    from meridian.lib.harness.extractors.base import AttemptFold
    from meridian.lib.harness.transcript import TranscriptEventParser
    from meridian.lib.launch.context import PreparedLaunchSurface

AdapterSpecT = TypeVar("AdapterSpecT", bound=ResolvedLaunchSpec, covariant=True)


def _empty_metadata() -> dict[str, object]:
    return {}


def _empty_env_overrides() -> dict[str, str]:
    return {}


class BootstrapMode(StrEnum):
    """How one harness boots or materializes interactive primary runs."""

    SUBPROCESS_ONLY = "subprocess_only"
    MANAGED_PRIMARY_ATTACH = "managed_primary_attach"


class HarnessCapabilities(BaseModel):
    """Feature flags for one harness implementation."""

    model_config = ConfigDict(frozen=True)

    supports_stdin_prompt: bool = False
    supports_session_resume: bool = False
    supports_session_fork: bool = False
    # The harness's native agent definition carries the profile body; otherwise the body
    # goes in the system prompt.
    native_agent_carries_body: bool = False
    supports_named_primary_resume: bool = False
    # Whether primary launch needs a synthetic first user prompt to bootstrap
    # one session. Harnesses that can attach without a first-turn prompt keep
    # this disabled.
    requires_initial_prompt: bool = False

    supports_native_file_injection: bool = False
    # Whether a black-box primary launches with captured output in its
    # non-interactive print mode (Claude `--print`).
    captures_blackbox_output: bool = False
    terminal_surface_mode: TerminalSurfaceMode = TerminalSurfaceMode.PTY_MEDIATED


class WorkspaceRoots(StrEnum):
    """How a harness takes extra workspace roots."""

    NONE = "none"
    # `--add-dir <root>` arguments on the launch command.
    ADD_DIR_ARGS = "add_dir_args"
    # The adapter's `project_launch_env` carries the roots.
    ENV = "env"


class HarnessContract(BaseModel):
    """Inspectable contract surface for one harness adapter."""

    model_config = ConfigDict(frozen=True)

    capabilities: HarnessCapabilities
    transport_ids: tuple[TransportId, ...]
    launch_spec_cls: str
    bootstrap_mode: BootstrapMode
    workspace_roots: WorkspaceRoots = WorkspaceRoots.NONE
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
    #: Whether the harness sends requests to Meridian over its server connection
    #: (approvals and the like), so a headless spawn needs a ``PermissionBroker``.
    #: Static: ``supports_runtime_hitl`` only exists once the connection does.
    sends_server_requests: bool = False
    #: Environment names (case-insensitive) no child inherits, whichever harness the
    #: child runs: the parent's own scope or secrets, not the child's to receive.
    forbidden_child_env_keys: frozenset[str] = frozenset()
    #: Same, for every name starting with one of these (case-insensitive).
    forbidden_child_env_prefixes: tuple[str, ...] = ()


class SpawnUsageContractVariants(BaseModel):
    """Harness-specific phrasing for the shared spawn-usage contract template."""

    model_config = ConfigDict(frozen=True)

    intro_line: str
    double_wrap_bullet: str
    timeout_bullet: str
    # Only where the harness reports its own work: elsewhere a child is not woken.
    waiting_line: str | None = None


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
    adhoc_agent_payload: str = ""
    extra_args: tuple[str, ...] = ()
    control_root: str | None = None
    task_cwd: str | None = None
    projected_roots: tuple[Path, ...] = ()
    # ``projected_roots`` without Meridian's own runtime, temp and socket roots.
    workspace_roots: tuple[Path, ...] = ()
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
    # In-memory facts for this launch's primary message route (a per-launch secret, say):
    # never serialized, logged or written to disk.
    route_payload: object | None = Field(default=None, repr=False, exclude=True)


class NativePrimaryRuntimeMetadata(BaseModel):
    """Harness-specific runtime fields projected into primary_meta.json."""

    model_config = ConfigDict(frozen=True)

    runtime_kind: str | None = None
    runtime_path: str | None = None
    runtime_version: str | None = None
    session_dir: str | None = None
    auth_policy: str | None = None


RecordConfigDirFn = Callable[[str], None]


@dataclass(frozen=True)
class NativeRelocation:
    """What relocate changes in a harness's own state for one chat.

    ``cwd_from`` is the directory the harness records for the chat and ``cwd_to`` the
    one relocate decided it moves to; the ops layer owns that rule, the harness only
    changes its own state. ``change`` states it in one line for the plan output.
    ``source`` is the native file the change acts on, chosen at plan time so apply and
    ``--dry-run`` name the same file. ``native_store`` is the chat's new store when the
    move changes it (Claude). A change the harness can't make is a plan with
    ``refusal`` set, not an exception.
    """

    identity: NativeKeyFields
    cwd_from: Path
    cwd_to: Path
    change: str
    source: Path | None = None
    native_store: str | None = None
    refusal: str | None = None


@dataclass(frozen=True)
class NativeRelocationResult:
    """The outcome of one applied plan; ``refusal`` is None when it took effect."""

    plan: NativeRelocation
    refusal: str | None = None


@dataclass(frozen=True)
class PreExecSessionRequest:
    """What an adapter's ``create_session_before_exec`` gets for one launch.

    ``operation`` is ``create`` for a fresh launch (``source_session_id`` is None) or
    ``fork`` (``source_session_id`` is the session to fork); a resume never reaches the
    port. ``surface`` holds the projected content, model selection and request; it is
    None for a spawn fork started by a background worker, which composes its surface
    after the fork. ``env`` is the base environment through ``inherit_child_env``, with
    no per-launch bindings. ``surface`` and ``env`` stay out of ``repr``: the request's
    environment overrides and the child environment can hold secrets.
    """

    operation: Literal["create", "fork"]
    source_session_id: str | None
    native_store: str | None
    surface: PreparedLaunchSurface | None = field(repr=False)
    cwd: Path
    env: dict[str, str] = field(repr=False)


@runtime_checkable
class SpawnExtractor(Protocol):
    """Attempt-local extraction interface for spawn finalization."""

    def create_fold(self, seed: Mapping[str, JsonValue] | None = None) -> AttemptFold: ...

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
        self, run: SpawnParams, *, precreated: PrecreatedSession | None = None
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

    def turn_since(self, key: NativeKey, *, started_at_epoch: float) -> bool | None: ...

    def native_observation_reader(
        self,
        *,
        child_env: Mapping[str, str],
        pid: int,
    ) -> Callable[[int], NativeObservationBatch] | None: ...

    def resolve_launch_spec(self, run: SpawnParams) -> AdapterSpecT: ...

    def launch_spec_warnings(self, spec: ResolvedLaunchSpec) -> tuple[CompositionWarning, ...]: ...

    def launch_files(self, spec: ResolvedLaunchSpec) -> dict[Path, str]: ...

    def launch_environment_warnings(
        self,
        *,
        child_env: Mapping[str, str],
        child_cwd: Path,
    ) -> tuple[CompositionWarning, ...]: ...


class TranscriptView(Protocol):
    """The events a harness reopens a session with, when that differs from the stored order."""

    @property
    def events(self) -> Sequence[dict[str, object]]: ...

    @property
    def view_basis(self) -> Literal["reopen-default"]: ...

    @property
    def reasons(self) -> tuple[str, ...]:
        """Why the source may be incomplete; empty when it is not."""
        ...


@runtime_checkable
class NativeStoreReader(Protocol):
    """Reading a harness's native sessions: the part of an adapter that needs no launch.

    Harnesses Meridian launches get it from their adapter; ones it never launches (``agy``)
    register just this, so ``session log``, search and ``--from`` need no special case.
    """

    # Sessions live together in one store (OpenCode's database), so search checks their
    # freshness with one ``session_witnesses`` call per store instead of a file stat each.
    native_store_shared: ClassVar[bool]

    def native_transcript_kind(self, path: Path) -> Literal["native_file", "native_db"]:
        """``native_db`` when ``path`` is a store shared by many sessions, read through
        ``open_session_snapshot``; ``native_file`` when it holds one session's events."""
        ...

    def session_witnesses(
        self, native_store: Path, session_ids: Iterable[str]
    ) -> Mapping[str, Witness]:
        """The freshness of each listed session found in a shared store; only asked when
        ``native_store_shared``."""
        ...

    def open_session_snapshot(
        self, path: Path, session_id: str
    ) -> AbstractContextManager[tuple[Witness, Iterator[dict[str, object]]]]:
        """One session's witness and raw events from a ``native_db`` source, read in one
        snapshot; raises ``ValueError`` when the session is not there."""
        ...

    def reopen_projector(self) -> Callable[[str], TranscriptView] | None:
        """What turns a whole stored transcript into the view the harness reopens it with;
        None when the stored events, in order, are that view."""
        ...

    def transcript_parser(self) -> TranscriptEventParser:
        """Turn this harness's native transcript events into messages for ``session log``."""
        ...

    def capture_dialect(self, *, kind: str, path: Path | None) -> str | None:
        """Name of the native format a capture of this source seals; None when the harness
        has no native capture."""
        ...

    def capture_observer(self, *, session_id: str, dialect: str) -> CaptureObserver:
        """Decide from the records of a ``capture_dialect`` capture whether its tail is
        complete."""
        ...

    def resolve_native_session_file(
        self,
        *,
        session_id: str,
        native_store: Path,
    ) -> Path | None: ...

    def read_last_model(
        self, *, session_id: str, native: Path, tail_bytes: int | None
    ) -> str | None:
        """The last model the native session file ``native`` ran, verbatim; None if unread.

        ``tail_bytes`` limits a JSONL history to its last N bytes."""
        ...


@runtime_checkable
class SubprocessHarness(HarnessAdapter[ResolvedLaunchSpec], NativeStoreReader, Protocol):
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
        primary_mode: bool,
    ) -> dict[str, str]: ...

    def blocked_child_env_vars(self) -> frozenset[str]:
        """Names kept from this harness's own children only.

        ``HarnessContract.forbidden_child_env_*`` are kept from every child.
        """
        ...

    def prepare_prelaunch(
        self,
        *,
        launch: LaunchPaths,
        identity: NativeIdentity | None,
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
    ) -> tuple[str, ...]: ...

    def redact_primary_command(self, command: tuple[str, ...]) -> tuple[str, ...]:
        """Redact harness-specific secrets from an argv persisted to metadata."""
        ...

    def uses_native_primary_metadata(self) -> bool:
        """Return whether this harness writes native/black-box primary metadata."""
        ...

    def native_primary_runtime_metadata(
        self,
        state: HarnessPrelaunchState,
    ) -> NativePrimaryRuntimeMetadata: ...

    def build_primary_runtime_request_handler(
        self,
        *,
        launch: LaunchPaths,
        event_sink: Callable[[RawHarnessEvent], Awaitable[None]],
    ) -> ServerRequestHandler | None: ...

    def native_session_evidence(
        self, path: Path, *, recorded_session_id: str | None
    ) -> NativeSessionEvidence:
        """Read ``path`` as one of this harness's sessions for ``session repair``; raises
        ``InvalidNativeSession`` when it is not, or the harness has no such reader."""
        ...

    continues_in_source_store: ClassVar[frozenset[Operation]]
    # The harness's own TUI asks for a new directory when its recorded one is gone, so a
    # launch that relocate could not repair is still worth offering.
    tui_recovers_directory: ClassVar[bool]

    def classify_store_move(self, *, recorded: str, attempted: str) -> NativeStoreMove | None: ...

    def locate_native_session(
        self, *, session_id: str, recorded_store: Path
    ) -> tuple[Path, ...]: ...

    def native_cwd(
        self,
        identity: NativeKeyFields,
        *,
        recorded_roots: Sequence[Path] = (),
    ) -> Path | None: ...

    def plan_native_relocate(
        self,
        identity: NativeKeyFields,
        *,
        cwd_from: Path,
        cwd_to: Path,
        native: Path | None = None,
    ) -> NativeRelocation | None: ...

    def apply_native_relocations(
        self,
        plans: Sequence[NativeRelocation],
    ) -> tuple[NativeRelocationResult, ...]: ...

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

    def project_content(self, content: ComposedLaunchContent) -> ProjectedContent: ...

    def create_session_before_exec(self, request: PreExecSessionRequest) -> str | None: ...

    def owns_untracked_session(self, *, project_root: Path, session_ref: str) -> bool: ...

    def bind_launch_paths(
        self, spec: ResolvedLaunchSpec, *, launch: LaunchPaths, dry_run: bool
    ) -> ResolvedLaunchSpec:
        """``spec`` with the launch's artifact paths it reads (system prompt, settings,
        report file) bound in; unchanged for a harness that takes none."""
        ...

    def consumed_by_harness(
        self,
        runtime_root: Path,
        parent_spawn_id: str,
        *,
        on_read: Callable[[Path, str | None], None] | None = None,
    ) -> frozenset[str]:
        """Launch keys whose results this harness's own delivery recorded as reaching the
        parent, beyond the shared record ``spawn collect`` keeps; a damaged source is
        skipped and reported to ``on_read``."""
        ...

    def blocking_command_hold(self) -> AbstractContextManager[None]:
        """Held while a Meridian command blocks the calling process: a harness whose shell
        tasks can be backgrounded or reaped marks the task busy for that long."""
        ...

    def idle_mod_dir(self) -> str | None:
        """Directory of Meridian's idle mod for this harness's own TUI, for an opt-in
        outside Meridian; None when it has none."""
        ...
