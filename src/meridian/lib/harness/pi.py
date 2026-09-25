"""Pi CLI harness adapter."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import ClassVar, cast

from meridian.lib.config.settings import resolve_pi_harness_profile
from meridian.lib.core.domain import SpawnStatus, TokenUsage
from meridian.lib.core.native_identity import (
    NativeEntryMismatch,
    NativeIdentityPlan,
    NativeSessionUnavailable,
    RunBoundary,
)
from meridian.lib.core.types import HarnessId, SpawnId, TransportId
from meridian.lib.harness.adapter import (
    ApprovalContract,
    ArtifactStore,
    BaseHarnessAdapter,
    BootstrapContract,
    BootstrapMode,
    ExtractionContract,
    ForkMaterializationMode,
    HarnessCapabilities,
    HarnessContract,
    HarnessPrelaunchState,
    McpConfig,
    NativePrimaryRuntimeMetadata,
    PermissionResolver,
    PrimarySessionObservation,
    ProjectionContract,
    ProjectionMode,
    RecordConfigDirFn,
    SessionRequest,
    SpawnParams,
    TransportContract,
)
from meridian.lib.harness.bundle import (
    HarnessBundle,
    HarnessProjectionPorts,
    register_harness_bundle,
)
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.connections.pi_rpc import PiRpcConnection
from meridian.lib.harness.extractors.pi import PI_EXTRACTOR
from meridian.lib.harness.pi_boundary import read_boundary
from meridian.lib.harness.pi_identity import mint_session_id, resolve_session_file, verify_identity
from meridian.lib.harness.pi_lifecycle_events import redact_pi_command_for_history
from meridian.lib.harness.pi_paths import (
    pi_agent_dir_env_override,
    pi_meridian_state_dir_env_override,
    pi_spawn_session_root_env_override,
    resolve_pi_spawn_session_root,
    scope_pi_session_dir_for_spawn,
)
from meridian.lib.harness.pi_runtime_resolver import (
    PiRuntimeResolutionError,
    resolve_pi_runtime,
)
from meridian.lib.harness.projections.pi_extension_projection import (
    PiExtensionLaunchProfile,
    default_extra_extension_path,
    resolve_extra_pi_extension_entrypoints,
    resolve_pi_extension_entrypoints,
)
from meridian.lib.harness.projections.project_pi_native_tui import (
    project_pi_native_tui_spec_to_cli_args,
)
from meridian.lib.harness.projections.project_pi_rpc import (
    project_pi_spec_to_cli_args,
)
from meridian.lib.harness.semantics import (
    MERIDIAN_CONNECTION_CLOSED_EVENT,
    PI_CANCELLED_STOP_REASONS,
    EventSemantics,
    HarnessSemantics,
    TerminalEventOutcome,
    connection_closed_outcome,
    stringify_terminal_error,
)
from meridian.lib.launch.composition import (
    ComposedLaunchContent,
    ProjectedContent,
    ProjectionChannels,
    build_reference_routing,
    join_content_blocks,
    render_system_instruction_blocks,
    render_task_context,
)
from meridian.lib.launch.constants import (
    BASE_COMMAND_PI_SUBPROCESS,
    PI_RUNTIME_META_FILENAME,
    PRIMARY_BASE_COMMAND_PI,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec, TerminalSurfaceMode
from meridian.lib.safety.permissions import PermissionConfig
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.paths import spawn_log_subpath


def _write_pi_runtime_metadata_sidecar(
    *,
    runtime_root: Path,
    spawn_id: SpawnId,
    payload: dict[str, str | None],
) -> None:
    """Persist the resolved Pi runtime metadata sidecar for one spawn."""

    if payload.get("runtime_path") is None:
        return
    metadata_path = runtime_root / spawn_log_subpath(spawn_id) / PI_RUNTIME_META_FILENAME
    atomic_write_text(
        metadata_path,
        json.dumps({"schema_version": 1, **payload}, separators=(",", ":")) + "\n",
    )


def _project_pi_subprocess_cli_args(
    spec: ResolvedLaunchSpec,
    *,
    base_command: tuple[str, ...],
) -> list[str]:
    if spec.interactive:
        return project_pi_native_tui_spec_to_cli_args(spec, base_command=base_command)
    return project_pi_spec_to_cli_args(spec, base_command=base_command)


class PiAdapter(BaseHarnessAdapter[ResolvedLaunchSpec]):
    """Pi harness implementation for native installed ``pi`` launches."""

    BASE_COMMAND: ClassVar[tuple[str, ...]] = BASE_COMMAND_PI_SUBPROCESS
    PRIMARY_BASE_COMMAND: ClassVar[tuple[str, ...]] = PRIMARY_BASE_COMMAND_PI
    _CONSUMED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "prompt",
            "model",
            "effort",
            "extra_args",
            "control_root",
            "interactive",
            "continue_harness_session_id",
            "continue_fork",
            "appended_system_prompt",
            "user_turn_content",
            "mcp_tools",
            "projected_roots",
            "pi_harness_profile",
        }
    )
    _EXPLICITLY_IGNORED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "skills",
            "agent",
            "adhoc_agent_payload",
            "context_from_payload",
            "reference_items",
            "task_cwd",
            "claude_allow_builtin_agents",
        }
    )

    @property
    def id(self) -> HarnessId:
        return HarnessId.PI

    @property
    def contract(self) -> HarnessContract:
        return HarnessContract(
            capabilities=self.capabilities,
            transport=TransportContract(
                transport_ids=(TransportId.STREAMING,),
                observer_controller_required=False,
            ),
            projection=ProjectionContract(
                launch_spec_cls="ResolvedLaunchSpec",
                mode=ProjectionMode.SYSTEM_FIELD_WITH_USER_TURN,
            ),
            extraction=ExtractionContract(
                session_observation_order=("connection_session", "artifacts", "current_session"),
            ),
            approval=ApprovalContract(
                subprocess_permission_flags_projected_by_shared_policy=False,
            ),
            bootstrap=BootstrapContract(
                mode=BootstrapMode.SUBPROCESS_ONLY,
                fork_materialization=ForkMaterializationMode.NATIVE_CONTINUE_FORK,
                primary_stderr_log=True,
            ),
        )

    @property
    def consumed_fields(self) -> frozenset[str]:
        return self._CONSUMED_FIELDS

    @property
    def explicitly_ignored_fields(self) -> frozenset[str]:
        return self._EXPLICITLY_IGNORED_FIELDS

    @property
    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            supports_stream_events=True,
            supports_stdin_prompt=True,
            supports_session_resume=True,
            supports_session_fork=True,
            supports_native_skills=False,
            supports_native_agents=False,
            supports_primary_launch=True,
            supports_named_primary_resume=True,
            supports_native_file_injection=False,
            terminal_surface_modes=(
                TerminalSurfaceMode.PTY_MEDIATED,
                TerminalSurfaceMode.NATIVE_INHERIT,
            ),
            default_terminal_surface_mode=TerminalSurfaceMode.PTY_MEDIATED,
        )

    def plan_native_identity(self, run: SpawnParams) -> NativeIdentityPlan:
        source_id = (run.continue_harness_session_id or "").strip() or None
        operation = (
            "fork" if source_id and run.continue_fork else "resume" if source_id else "create"
        )
        return NativeIdentityPlan(
            source_id if operation == "resume" else None, None, source_id, operation
        )

    def finalize_native_identity(
        self,
        plan: NativeIdentityPlan,
        *,
        child_env: dict[str, str],
        child_cwd: Path,
        session: SessionRequest,
        spawn_id: SpawnId,
        interactive: bool,
    ) -> NativeIdentityPlan:
        store = resolve_pi_spawn_session_root(env=child_env)
        if not store.is_absolute():
            store = child_cwd / store
        source_store = session.source_native_store
        source_path = None
        if plan.operation != "create":
            if session.continue_source_tracked and not source_store:
                raise NativeSessionUnavailable(
                    session.continue_source_ref or session.requested_harness_session_id or "source",
                    "unbound",
                )
            assert plan.locator is not None
            source_path = resolve_session_file(
                Path(source_store) if source_store else store, plan.locator
            )
        if plan.operation == "resume":
            assert source_path is not None
            store = source_path.parent
        elif not interactive:
            child_env["PI_CODING_AGENT_SESSION_DIR"] = str(store)
            store = Path(scope_pi_session_dir_for_spawn(child_env=child_env, spawn_id=spawn_id))
        store = store.resolve()
        child_env["PI_CODING_AGENT_SESSION_DIR"] = str(store)
        session_id = (
            plan.harness_session_id if plan.operation == "resume" else mint_session_id(store)
        )
        return NativeIdentityPlan(
            session_id, str(store), str(source_path) if source_path else None, plan.operation
        )

    def verify_native_identity(
        self, plan: NativeIdentityPlan,
    ) -> NativeEntryMismatch | NativeSessionUnavailable | None:
        try:
            verify_identity(plan)
        except (NativeEntryMismatch, NativeSessionUnavailable) as exc:
            return exc
        return None

    def resolve_launch_spec(
        self,
        run: SpawnParams,
        perms: PermissionResolver,
    ) -> ResolvedLaunchSpec:
        continue_session_id = (run.continue_harness_session_id or "").strip() or None
        if run.pi_harness_profile is not None:
            pi_profile = run.pi_harness_profile
        else:
            control_root = (run.control_root or "").strip()
            pi_profile = resolve_pi_harness_profile(
                project_root=Path(control_root).expanduser().resolve() if control_root else None,
            )
        meridian_entrypoints = resolve_pi_extension_entrypoints(
            PiExtensionLaunchProfile(
                background_tasks_enabled=pi_profile.background_tasks_enabled(),
                spawn_watch_enabled=pi_profile.spawn_watch.enabled,
                interactive=run.interactive,
            ),
        )
        extra_entrypoints: tuple[str, ...] = ()
        if pi_profile.load_all_pi_extensions:
            extra_roots = (
                tuple(Path(path).expanduser() for path in pi_profile.extra_extension_paths)
                if pi_profile.extra_extension_paths
                else (default_extra_extension_path(),)
            )
            extra_entrypoints = resolve_extra_pi_extension_entrypoints(extra_roots)
        entrypoints = meridian_entrypoints + extra_entrypoints
        return ResolvedLaunchSpec(
            harness=HarnessId.PI,
            native_identity_plan=self.plan_native_identity(run),
            model=str(run.model).strip() if run.model else None,
            effort=run.effort,
            prompt=run.user_turn_content or run.prompt,
            continue_session_id=continue_session_id,
            continue_fork=run.continue_fork and continue_session_id is not None,
            permission_resolver=perms,
            extra_args=run.extra_args,
            interactive=run.interactive,
            mcp_tools=run.mcp_tools,
            projected_roots=run.projected_roots,
            appended_system_prompt=run.appended_system_prompt,
            pi_extension_entrypoints=entrypoints,
            load_all_pi_extensions=pi_profile.load_all_pi_extensions,
            agent_name=None,
            skills=(),
        )

    def build_command(self, run: SpawnParams, perms: PermissionResolver) -> list[str]:
        spec = self.resolve_launch_spec(run, perms)
        base_command = self.PRIMARY_BASE_COMMAND if spec.interactive else self.BASE_COMMAND
        return _project_pi_subprocess_cli_args(spec, base_command=base_command)

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
            spawn_id,
            session,
            resolved_harness_session_id,
            record_effective_config_dir,
        )
        role = child_env.get("_MERIDIAN_PI_SESSION_ROLE", "").strip().lower()
        launch_role = "primary" if role == "primary" else "spawned"
        try:
            resolved_runtime = resolve_pi_runtime(env=child_env, role=launch_role)
        except PiRuntimeResolutionError:
            raise
        except Exception as exc:
            raise PiRuntimeResolutionError(str(exc)) from exc

        session_dir = child_env["PI_CODING_AGENT_SESSION_DIR"]
        agent_dir = child_env.get("PI_CODING_AGENT_DIR", "").strip()
        state_dir_overrides = pi_meridian_state_dir_env_override(
            env=child_env,
            runtime_root=runtime_root,
        )
        child_env.update(state_dir_overrides)
        env_overrides: dict[str, str] = {
            "MERIDIAN_PI_BINARY": resolved_runtime.binary_path,
            "_MERIDIAN_PI_SESSION_BOUNDARY_PATH": str(
                (runtime_root / spawn_log_subpath(spawn_id) / "pi-session-boundary.json").resolve()
            ),
            "_MERIDIAN_PI_SESSION_BOUNDARY_NONCE": secrets.token_hex(32),
        }

        _write_pi_runtime_metadata_sidecar(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            payload={
                "runtime_kind": resolved_runtime.runtime_kind,
                "runtime_path": resolved_runtime.binary_path,
                "runtime_version": resolved_runtime.runtime_version,
                "session_dir": session_dir,
                "auth_policy": "shared-pi-agent-dir",
            },
        )
        return HarnessPrelaunchState(
            env_overrides=env_overrides,
            metadata={
                "pi_runtime_kind": resolved_runtime.runtime_kind,
                "pi_runtime_path": resolved_runtime.binary_path,
                "pi_runtime_version": resolved_runtime.runtime_version,
                "pi_runtime_session_dir": session_dir,
                "pi_runtime_agent_dir": agent_dir,
                "pi_runtime_auth_policy": "shared-pi-agent-dir",
            },
        )

    def observe_run_boundary(
        self, *, child_env: dict[str, str], pid: int | None,
    ) -> RunBoundary:
        path = child_env.get("_MERIDIAN_PI_SESSION_BOUNDARY_PATH")
        nonce = child_env.get("_MERIDIAN_PI_SESSION_BOUNDARY_NONCE")
        if not path or not nonce:
            return RunBoundary()
        return read_boundary(Path(path), nonce=nonce, pid=pid)

    def uses_native_primary_metadata(self) -> bool:
        return self.contract.bootstrap.mode is BootstrapMode.SUBPROCESS_ONLY

    def native_primary_runtime_metadata(
        self,
        state: HarnessPrelaunchState,
    ) -> NativePrimaryRuntimeMetadata:
        def _text(field: str) -> str | None:
            raw = state.metadata.get(field)
            if not isinstance(raw, str):
                return None
            return raw.strip() or None

        return NativePrimaryRuntimeMetadata(
            runtime_kind=_text("pi_runtime_kind"),
            runtime_path=_text("pi_runtime_path"),
            runtime_version=_text("pi_runtime_version"),
            session_dir=_text("pi_runtime_session_dir"),
            auth_policy=_text("pi_runtime_auth_policy"),
        )

    def resolve_primary_command(
        self,
        command: tuple[str, ...],
        *,
        state: HarnessPrelaunchState,
    ) -> tuple[str, ...]:
        if not command:
            return command
        runtime_path = (state.metadata.get("pi_runtime_path") or "").strip()
        if not runtime_path:
            return command
        return (runtime_path, *command[1:])

    def redact_primary_command(self, command: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(redact_pi_command_for_history(command))

    def observe_primary_session_id(
        self,
        *,
        native_identity_plan: NativeIdentityPlan | None,
        command: tuple[str, ...],
        child_env: dict[str, str],
        launch_child_cwd: Path,
        started_at_epoch: float | None,
        expected_session_id: str,
        requested_session_id: str,
        resolved_session_id: str,
        exit_code: int,
    ) -> PrimarySessionObservation:
        assert native_identity_plan is not None
        try:
            status = verify_identity(native_identity_plan)
        except ValueError as exc:
            return PrimarySessionObservation(discovery="conflict", detail=str(exc))
        return PrimarySessionObservation(
            session_id=native_identity_plan.harness_session_id, discovery=status,
        )

    def mcp_config(self, run: SpawnParams) -> McpConfig | None:
        _ = run
        return None

    def project_content(self, content: ComposedLaunchContent) -> ProjectedContent:
        system_prompt = render_system_instruction_blocks(content)
        reference_routing = build_reference_routing(content.reference_items)
        task_context = render_task_context(
            content.reference_items,
            reference_routing,
            content.prior_output,
        )
        user_turn = join_content_blocks(task_context, content.user_task_prompt)

        return ProjectedContent(
            system_prompt=system_prompt,
            user_turn_content=user_turn,
            reference_routing=reference_routing,
            channels=ProjectionChannels(
                system_instruction="system-field" if system_prompt.strip() else "none",
                user_task_prompt="user-turn",
                task_context="user-turn",
            ),
        )

    def env_overrides(self, config: PermissionConfig) -> dict[str, str]:
        _ = config
        return {
            **pi_agent_dir_env_override(),
            **pi_spawn_session_root_env_override(),
        }

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
        return PI_EXTRACTOR.extract_usage(artifacts, spawn_id)

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        return PI_EXTRACTOR.extract_session_id(artifacts, spawn_id)

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        return PI_EXTRACTOR.extract_report(artifacts, spawn_id)

    def resolve_session_file(
        self,
        *,
        project_root: Path,
        session_id: str,
        config_root_hint: Path | None = None,
    ) -> Path | None:
        if config_root_hint is None:
            return None
        return resolve_session_file(config_root_hint, session_id, pending=True)

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
        def _norm(value: str | None) -> str | None:
            if not value:
                return None
            stripped = value.strip()
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

        return None


def _resolve_pi_terminal(event: RawHarnessEvent) -> TerminalEventOutcome | None:
    if event.event_type == MERIDIAN_CONNECTION_CLOSED_EVENT:
        return connection_closed_outcome(event)
    if event.event_type == "response":
        command = str(event.payload.get("command", "")).strip().lower()
        is_inject_response = event.payload.get("meridian_control_action") == "inject"
        if command == "prompt" and event.payload.get("success") is False and not is_inject_response:
            error = stringify_terminal_error(event.payload.get("error")) or "pi_prompt_rejected"
            return TerminalEventOutcome(status=SpawnStatus.FAILED, exit_code=1, error=error)
        return None

    messages_obj = event.payload.get("messages")
    if isinstance(messages_obj, list):
        for message_obj in reversed(cast("list[object]", messages_obj)):
            if not isinstance(message_obj, dict):
                continue
            message = cast("dict[str, object]", message_obj)
            if str(message.get("role", "")).strip().lower() != "assistant":
                continue
            stop_reason = str(message.get("stopReason", "")).strip().lower()
            if stop_reason == "error":
                return TerminalEventOutcome(
                    status=SpawnStatus.FAILED, exit_code=1, error="pi_stop_error"
                )
            if stop_reason in PI_CANCELLED_STOP_REASONS:
                return TerminalEventOutcome(
                    status=SpawnStatus.CANCELLED, exit_code=130, error="cancelled"
                )
            break
    return TerminalEventOutcome(status=SpawnStatus.SUCCEEDED, exit_code=0)


PI_SEMANTICS = HarnessSemantics(
    events={
        "agent_start": EventSemantics(activity="turn_active"),
        "turn_start": EventSemantics(activity="turn_active"),
        "message_start": EventSemantics(activity="turn_active"),
        "message_update": EventSemantics(activity="turn_active"),
        "tool_execution_start": EventSemantics(activity="turn_active"),
        "tool_execution_update": EventSemantics(activity="turn_active"),
        "turn_end": EventSemantics(activity="idle"),
        "agent_end": EventSemantics(activity="idle", clears_signal=True),
        "response": EventSemantics(),
        MERIDIAN_CONNECTION_CLOSED_EVENT: EventSemantics(),
    },
    payload_resolvers={
        "agent_end": _resolve_pi_terminal,
        "response": _resolve_pi_terminal,
        MERIDIAN_CONNECTION_CLOSED_EVENT: _resolve_pi_terminal,
    },
)

register_harness_bundle(
    HarnessBundle(
        harness_id=HarnessId.PI,
        adapter=PiAdapter(),
        spec_cls=ResolvedLaunchSpec,
        extractor=PI_EXTRACTOR,
        connections={TransportId.STREAMING: PiRpcConnection},
        projections=HarnessProjectionPorts(
            subprocess_cli_args=_project_pi_subprocess_cli_args,
        ),
        semantics=PI_SEMANTICS,
    )
)
