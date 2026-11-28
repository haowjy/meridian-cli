"""Shared defaults adapters build on; callers outside ``lib/harness/`` use ``adapter``."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Generic, Literal, final

from meridian.lib.core.native_identity import (
    InvalidNativeSession,
    LaunchIntent,
    NativeIdentity,
    NativeKey,
    NativeKeyFields,
    NativeObservationBatch,
    NativeSessionEvidence,
    NativeSessionUnavailable,
    NativeStoreMove,
    Operation,
    PostExit,
    PrecreatedSession,
)
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.adapter import (
    HarnessContract,
    HarnessPrelaunchState,
    NativePrimaryRuntimeMetadata,
    NativeRelocation,
    NativeRelocationResult,
    PreExecSessionRequest,
    RecordConfigDirFn,
    RunPromptPolicy,
    SpawnParams,
)
from meridian.lib.harness.connections.base import RawHarnessEvent, ServerRequestHandler
from meridian.lib.harness.launch_types import SessionSeed
from meridian.lib.launch.composition import (
    ComposedLaunchContent,
    ProjectedContent,
    project_inline_content,
)
from meridian.lib.launch.launch_types import CompositionWarning, ResolvedLaunchSpec, SpecT
from meridian.lib.launch.request import SessionRequest, is_exact_continue_session
from meridian.lib.safety.policy import NetworkPolicy
from meridian.lib.state.paths import LaunchPaths

if TYPE_CHECKING:
    from meridian.lib.harness.capture_qualify import CaptureObserver
    from meridian.lib.harness.transcript import TranscriptEventParser


class BaseNativeStoreReader:
    """Defaults for a ``NativeStoreReader``: a JSONL file, shape-based parsing, no capture."""

    def native_transcript_kind(self, path: Path) -> Literal["native_file", "opencode_db"]:
        return "native_file"

    def transcript_parser(self) -> TranscriptEventParser:
        from meridian.lib.harness.transcript import DefaultTranscriptEventParser

        return DefaultTranscriptEventParser()

    def capture_dialect(self, *, kind: str, path: Path | None) -> str | None:
        _ = kind, path
        return None

    def capture_observer(self, *, session_id: str, dialect: str) -> CaptureObserver:
        raise NotImplementedError(
            f"{type(self).__name__} declares capture dialect {dialect!r} but no observer"
        )

    def resolve_native_session_file(
        self,
        *,
        session_id: str,
        native_store: Path,
    ) -> Path | None:
        return None

    def read_last_model(
        self, *, session_id: str, native: Path, tail_bytes: int | None
    ) -> str | None:
        _ = session_id, native, tail_bytes
        return None


class BaseHarnessAdapter(BaseNativeStoreReader, Generic[SpecT], ABC):
    """Base adapter with contract enforcement and optional helper defaults."""

    @property
    @abstractmethod
    def id(self) -> HarnessId: ...

    @property
    @abstractmethod
    def contract(self) -> HarnessContract: ...

    @property
    @abstractmethod
    def consumed_fields(self) -> frozenset[str]: ...

    @property
    @abstractmethod
    def explicitly_ignored_fields(self) -> frozenset[str]: ...

    @property
    def handled_fields(self) -> frozenset[str]:
        return self.consumed_fields | self.explicitly_ignored_fields

    native_identity: ClassVar[bool] = False
    refused_identity_flags: ClassVar[frozenset[str]] = frozenset()
    continues_in_source_store: ClassVar[frozenset[Operation]] = frozenset()
    tui_recovers_directory: ClassVar[bool] = False
    native_transcript_choice: ClassVar[bool] = False
    resolves_untracked_source: ClassVar[bool] = False

    @final
    def plan_native_identity(
        self,
        run: SpawnParams,
        *,
        precreated: PrecreatedSession | None = None,
    ) -> LaunchIntent | None:
        if not self.native_identity:
            return None
        for token in run.extra_args:
            if token.split("=", 1)[0] in self.refused_identity_flags:
                raise ValueError(
                    f"{self.id} managed identity refuses {token} in passthrough extra_args"
                )
        source = (run.continue_harness_session_id or "").strip() or None
        if precreated is not None:
            intent = LaunchIntent(precreated.operation, precreated_session_id=precreated.session_id)
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
        source_store = session.source_native_store if op != "create" else None
        if op != "create" and source_store is None and session.continue_source_tracked:
            raise NativeSessionUnavailable(ref, "unbound")
        if source_store is not None and op in self.continues_in_source_store:
            self.pin_native_store(child_env, source_store)
        store = self._store(child_env, child_cwd, spawn_id, op, interactive)
        # A resume into its own chat must run in the recorded store: bind_entry refuses
        # any other, later and untyped. A resume landing in a fresh chat has no recorded
        # store to keep.
        resumes_chat = op == "resume" and _resumes_source_chat(session)
        if (
            source_store is not None
            and store != source_store
            and (resumes_chat or op in self.continues_in_source_store)
        ):
            move = (
                self.classify_store_move(recorded=source_store, attempted=store)
                if resumes_chat
                else None
            )
            if move is None:
                raise NativeSessionUnavailable(ref, "missing")
            raise NativeSessionUnavailable(ref, "store_moved", store_move=move)
        source: Path | None = None
        located_from: Path | None = None
        if op != "create" and (source_store is not None or self.resolves_untracked_source):
            lookup = source_store or self._store(
                child_env, child_cwd, spawn_id, "resume", interactive
            )
            wanted = intent.source_session_id or intent.precreated_session_id
            source = self._require_source(
                wanted,
                Path(lookup),
                ref=ref,
                # A tracked chat resumed in its recorded store: the file may have gone
                # missing from it. Nothing else has a recorded place to search around.
                search_chat=session.continue_chat_id
                if resumes_chat and source_store is not None and session.continue_source_tracked
                else None,
            )
            located_from = source if source.parent != Path(lookup) else None
        self.pin_native_store(child_env, store)
        return NativeIdentity(
            str(self.id),
            op,
            store,
            self.assign_session_id(intent, store=Path(store)),
            intent.source_session_id,
            source,
            located_from,
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

    def _require_source(
        self, session_id: str | None, store: Path, *, ref: str, search_chat: str | None = None
    ) -> Path:
        """The native file for the session. ``search_chat`` names the chat whose recorded
        file is missing from ``store``: then one other file for the ID is found and used,
        and several are listed, never picked between. Nothing is saved."""
        if session_id is None:
            raise NativeSessionUnavailable(ref, "unbound")
        try:
            source = self.resolve_native_session_file(session_id=session_id, native_store=store)
        except NativeSessionUnavailable as exc:
            raise exc.for_ref(ref) from exc
        if source is not None:
            return source
        found = (
            self.locate_native_session(session_id=session_id, recorded_store=store)
            if search_chat is not None
            else ()
        )
        if len(found) == 1:
            return found[0]
        if found:
            listing = "".join(f"\n  {path}" for path in found)
            raise NativeSessionUnavailable(
                ref,
                "ambiguous_native_file",
                hint=f"{session_id} is missing from {store}, and {len(found)} copies exist:"
                f"{listing}\nPick one: meridian session relocate {search_chat} --native <path>",
            )
        raise NativeSessionUnavailable(ref, "missing")

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
            else intent.precreated_session_id
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

    def turn_since(self, key: NativeKey, *, started_at_epoch: float) -> bool | None:
        """Whether the native session ``key`` gained a user-visible turn since launch.

        Only picks the exit message of an interactive resume, so it must never raise:
        ``None`` means no reliable signal (the default).
        """
        _ = key, started_at_epoch
        return None

    def native_observation_reader(
        self,
        *,
        child_env: Mapping[str, str],
        pid: int,
    ) -> Callable[[int], NativeObservationBatch] | None:
        """Capture a launch-owned reader, or opt out without starting a drainer."""
        return None

    @abstractmethod
    def resolve_launch_spec(self, run: SpawnParams) -> SpecT: ...

    def launch_spec_warnings(self, spec: ResolvedLaunchSpec) -> tuple[CompositionWarning, ...]:

        _ = spec
        return ()

    def launch_files(self, spec: ResolvedLaunchSpec) -> dict[Path, str]:
        """Files the projected command reads, by path; written with the launch artifacts."""
        _ = spec
        return {}

    def launch_environment_warnings(
        self,
        *,
        child_env: Mapping[str, str],
        child_cwd: Path,
    ) -> tuple[CompositionWarning, ...]:

        _ = child_env, child_cwd
        return ()

    def reject_removed_native_config(self, native_config: Mapping[str, object]) -> None:

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

    def create_session_before_exec(self, request: PreExecSessionRequest) -> str | None:
        """Create the native session Meridian launches on, or ``None`` to let the harness
        create its own.

        Runs after the spawn row exists and before the harness execs: on a fresh primary
        launch (``create``) and on a fork of either kind (``fork``), never on a resume. A
        returned ID is recorded on the row and launched on; an exception fails the launch.
        """
        _ = request
        return None

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
        primary_mode: bool,
    ) -> dict[str, str]:
        """Env overrides that deliver the bound prompt, model, roots and permissions.

        ``primary_mode`` says the launched agent's profile declares ``mode: primary``;
        adapters that key policy off agent lifetime read it, others ignore it.

        ``system_prompt_path`` is where the appended prompt is written, after bind and
        before launch, whenever ``spec.appended_system_prompt`` is non-empty; ``env`` is
        the child env as bound.
        """
        _ = spec, system_prompt_path, env, primary_mode
        return {}

    def prepare_prelaunch(
        self,
        *,
        launch: LaunchPaths,
        identity: NativeIdentity | None,
        child_cwd: Path,
        child_env: dict[str, str],
        resolved_harness_session_id: str,
        record_effective_config_dir: RecordConfigDirFn | None = None,
    ) -> HarnessPrelaunchState:
        _ = (
            launch,
            identity,
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
        launch: LaunchPaths,
        event_sink: Callable[[RawHarnessEvent], Awaitable[None]],
    ) -> ServerRequestHandler | None:
        _ = launch, event_sink
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
        return project_inline_content(content)

    def native_session_evidence(
        self, path: Path, *, recorded_session_id: str | None
    ) -> NativeSessionEvidence:
        _ = path, recorded_session_id
        raise InvalidNativeSession(f"session repair does not support {self.id} chats")

    def classify_store_move(self, *, recorded: str, attempted: str) -> NativeStoreMove | None:
        """Why a resume's store differs from the recorded one; only the adapter knows
        its store layout, and so how to fix a config-root difference (``fix``). None when
        the difference is no move: the launch could not pin the recorded store, and the
        resume reports the transcript as missing."""
        _ = recorded, attempted
        return None

    def locate_native_session(self, *, session_id: str, recorded_store: Path) -> tuple[Path, ...]:
        """Distinct native files for ``session_id`` outside ``recorded_store`` where the
        recorded file is missing, searched by ID alone within the recorded config root;
        paths that are one file collapse. A harness whose transcript never leaves its
        recorded store has nothing to search: the default."""
        _ = session_id, recorded_store
        return ()

    def native_cwd(
        self,
        identity: NativeKeyFields,
        *,
        recorded_roots: Sequence[Path] = (),
    ) -> Path | None:
        """The directory this harness resumes the session in, from its own record.

        ``recorded_roots`` are the chat's generation control roots, newest first, for a
        harness that can only confirm a recorded root against its own state. None when
        the harness keeps no such record or the record is absent.
        """
        _ = identity, recorded_roots
        return None

    def plan_native_relocate(
        self,
        identity: NativeKeyFields,
        *,
        cwd_from: Path,
        cwd_to: Path,
        native: Path | None = None,
    ) -> NativeRelocation | None:
        """What relocate changes in this harness's own state for one chat, or None.

        Called only when the harness's own directory (``native_cwd``) is ``cwd_from`` and
        relocate decided it becomes ``cwd_to``. ``native`` is the user's ``--native``
        file: refused here unless ``native_transcript_choice`` says the harness can use
        one, so a harness never ignores it. Override ``_plan_native_relocate``, not this.
        """
        if native is not None and not self.native_transcript_choice:
            return NativeRelocation(
                identity, cwd_from, cwd_to, "", refusal=f"--native is not supported for {self.id}"
            )
        return self._plan_native_relocate(identity, cwd_from=cwd_from, cwd_to=cwd_to, native=native)

    def _plan_native_relocate(
        self,
        identity: NativeKeyFields,
        *,
        cwd_from: Path,
        cwd_to: Path,
        native: Path | None,
    ) -> NativeRelocation | None:
        _ = identity, cwd_from, cwd_to, native
        return None

    def apply_native_relocations(
        self,
        plans: Sequence[NativeRelocation],
    ) -> tuple[NativeRelocationResult, ...]:
        """Apply every plan for this harness in one call; idempotent. Receives only
        plans without a refusal. A failure is a result with a refusal, per plan. The
        default has nothing to change."""
        return tuple(NativeRelocationResult(plan) for plan in plans)

    def resolve_session_file(
        self,
        *,
        project_root: Path,
        session_id: str,
        config_root_hint: Path | None = None,
    ) -> Path | None:
        _ = project_root, session_id, config_root_hint
        return None


def _resumes_source_chat(session: SessionRequest) -> bool:
    return bool(session.continue_chat_id) and is_exact_continue_session(session)
