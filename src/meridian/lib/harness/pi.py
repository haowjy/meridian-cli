"""Pi CLI harness adapter."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from meridian.lib.config.settings import resolve_pi_harness_profile
from meridian.lib.core.domain import TokenUsage
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
    PermissionResolver,
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
from meridian.lib.harness.connections.pi_rpc import PiRpcConnection
from meridian.lib.harness.extractors.pi import (
    PI_EXTRACTOR,
    detect_pi_session_id_from_session_files,
)
from meridian.lib.harness.pi_paths import (
    pi_agent_dir_env_override,
    pi_meridian_state_dir_env_override,
    pi_spawn_session_root_env_override,
    resolve_pi_spawn_session_root,
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
from meridian.lib.launch.composition import (
    ComposedLaunchContent,
    ProjectedContent,
    ProjectionChannels,
    build_reference_routing,
    join_content_blocks,
    render_system_instruction_blocks,
    render_task_context,
)
from meridian.lib.launch.constants import BASE_COMMAND_PI_SUBPROCESS, PRIMARY_BASE_COMMAND_PI
from meridian.lib.launch.env import scope_pi_session_dir_for_spawn
from meridian.lib.launch.launch_types import ResolvedLaunchSpec, TerminalSurfaceMode
from meridian.lib.safety.permissions import PermissionConfig


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
            "report_output_path",
            "context_from_payload",
            "reference_items",
            "task_cwd",
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
                session_observation_order=("artifacts", "primary_detection", "current_session"),
            ),
            approval=ApprovalContract(
                subprocess_permission_flags_projected_by_shared_policy=False,
            ),
            bootstrap=BootstrapContract(
                mode=BootstrapMode.SUBPROCESS_ONLY,
                fork_materialization=ForkMaterializationMode.NATIVE_CONTINUE_FORK,
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
            supports_native_file_injection=False,
            terminal_surface_modes=(
                TerminalSurfaceMode.PTY_MEDIATED,
                TerminalSurfaceMode.NATIVE_INHERIT,
            ),
            default_terminal_surface_mode=TerminalSurfaceMode.PTY_MEDIATED,
        )

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
                project_root=Path(control_root).expanduser().resolve()
                if control_root
                else None,
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
        role = child_env.get("MERIDIAN_PI_SESSION_ROLE", "").strip().lower()
        launch_role = "primary" if role == "primary" else "spawned"
        try:
            resolved_runtime = resolve_pi_runtime(env=child_env, role=launch_role)
        except PiRuntimeResolutionError:
            raise
        except Exception as exc:
            raise PiRuntimeResolutionError(str(exc)) from exc

        scoped_session_dir: str | None = None
        if launch_role == "spawned":
            scoped_session_dir = scope_pi_session_dir_for_spawn(
                child_env=child_env,
                spawn_id=spawn_id,
            )
        elif launch_role == "primary":
            source_session_dir = (session.source_pi_session_dir or "").strip()
            if source_session_dir:
                child_env["PI_CODING_AGENT_SESSION_DIR"] = source_session_dir
                scoped_session_dir = source_session_dir

        session_dir = child_env.get("PI_CODING_AGENT_SESSION_DIR", "").strip() or str(
            resolve_pi_spawn_session_root(env=child_env)
        )
        if scoped_session_dir is not None:
            session_dir = scoped_session_dir
        agent_dir_overrides = pi_agent_dir_env_override()
        child_env.update(agent_dir_overrides)
        state_dir_overrides = pi_meridian_state_dir_env_override(
            env=child_env,
            runtime_root=runtime_root,
        )
        child_env.update(state_dir_overrides)
        env_overrides: dict[str, str] = {
            "MERIDIAN_PI_BINARY": resolved_runtime.binary_path,
            **agent_dir_overrides,
        }
        if scoped_session_dir is not None:
            env_overrides["PI_CODING_AGENT_SESSION_DIR"] = scoped_session_dir

        return HarnessPrelaunchState(
            env_overrides=env_overrides,
            metadata={
                "pi_runtime_kind": resolved_runtime.runtime_kind,
                "pi_runtime_path": resolved_runtime.binary_path,
                "pi_runtime_version": resolved_runtime.runtime_version,
                "pi_runtime_session_dir": session_dir,
                "pi_runtime_agent_dir": agent_dir_overrides["PI_CODING_AGENT_DIR"],
                "pi_runtime_auth_policy": "shared-pi-agent-dir",
            },
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

    def resolve_session_file(self, *, project_root: Path, session_id: str) -> Path | None:
        _ = project_root  # pi sessions are user-scoped, not project-scoped
        normalized = session_id.strip()
        if not normalized:
            return None
        root = resolve_pi_spawn_session_root()
        if not root.is_dir():
            return None
        # {timestamp}_{id}.jsonl in root, or {spawn_id}/{timestamp}_{id}.jsonl
        for pattern in (f"*_{normalized}.jsonl", f"*/*_{normalized}.jsonl"):
            for candidate in root.glob(pattern):
                if candidate.is_file():
                    return candidate
        return None

    def owns_untracked_session(self, *, project_root: Path, session_ref: str) -> bool:
        normalized_session_ref = session_ref.strip()
        if not normalized_session_ref:
            return False
        return (
            self.resolve_session_file(
                project_root=project_root,
                session_id=normalized_session_ref,
            )
            is not None
        )

    def detect_primary_session_id(
        self,
        *,
        project_root: Path,
        started_at_epoch: float,
        started_at_local_iso: str | None,
        expected_session_id: str | None = None,
    ) -> str | None:
        _ = started_at_local_iso
        return detect_pi_session_id_from_session_files(
            launch_env=pi_spawn_session_root_env_override(),
            child_cwd=project_root,
            started_at_epoch=started_at_epoch,
            expected_session_id=expected_session_id,
        )

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

        current = _norm(current_session_id)
        if current:
            return current

        return None


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
    )
)
