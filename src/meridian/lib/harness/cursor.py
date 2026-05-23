"""Cursor CLI harness adapter and bundle registration."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import ClassVar

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
    McpConfig,
    PermissionResolver,
    ProjectionContract,
    ProjectionMode,
    RunPromptPolicy,
    RuntimeHitlMode,
    SpawnParams,
    TransportContract,
)
from meridian.lib.harness.bundle import (
    HarnessBundle,
    HarnessProjectionPorts,
    project_subprocess_spec,
    register_harness_bundle,
)
from meridian.lib.harness.connections.cursor_subprocess import CursorSubprocessConnection
from meridian.lib.harness.extractors.cursor import CURSOR_EXTRACTOR
from meridian.lib.harness.projections.project_cursor import project_cursor_spec_to_cli_args
from meridian.lib.launch.constants import BASE_COMMAND_CURSOR_SUBPROCESS
from meridian.lib.launch.launch_types import (
    PreflightResult,
    ResolvedLaunchSpec,
    TerminalSurfaceMode,
)
from meridian.lib.safety.permissions import PermissionConfig


class CursorAdapter(BaseHarnessAdapter[ResolvedLaunchSpec]):
    """Subprocess harness implementation for Cursor's ``cursor agent`` CLI."""

    BASE_COMMAND: ClassVar[tuple[str, ...]] = BASE_COMMAND_CURSOR_SUBPROCESS
    _CONSUMED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "prompt",
            "model",
            "effort",
            "extra_args",
            "task_cwd",
            "mcp_tools",
            "projected_roots",
            "interactive",
        }
    )
    _EXPLICITLY_IGNORED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "skills",
            "agent",
            "adhoc_agent_payload",
            "control_root",
            "appended_system_prompt",
            "report_output_path",
            "user_turn_content",
            "context_from_payload",
            "reference_items",
            "continue_harness_session_id",
            "continue_fork",
        }
    )

    @property
    def id(self) -> HarnessId:
        return HarnessId.CURSOR

    @property
    def contract(self) -> HarnessContract:
        return HarnessContract(
            capabilities=self.capabilities,
            transport=TransportContract(
                transport_ids=(TransportId.SUBPROCESS,),
                observer_controller_required=False,
            ),
            projection=ProjectionContract(
                launch_spec_cls="ResolvedLaunchSpec",
                mode=ProjectionMode.POSITIONAL_PROMPT,
            ),
            extraction=ExtractionContract(
                session_observation_order=(
                    "artifacts",
                    "current_session",
                )
            ),
            approval=ApprovalContract(
                runtime_hitl=RuntimeHitlMode.NONE,
                subprocess_permission_flags_projected_by_shared_policy=False,
                default_runtime_request_policy="none",
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
            supports_stdin_prompt=False,
            supports_session_resume=False,
            supports_session_fork=False,
            supports_native_skills=False,
            supports_native_agents=False,
            supports_primary_launch=False,
            supports_native_file_injection=False,
            terminal_surface_modes=(TerminalSurfaceMode.PTY_MEDIATED,),
            default_terminal_surface_mode=TerminalSurfaceMode.PTY_MEDIATED,
        )

    def run_prompt_policy(self) -> RunPromptPolicy:
        return RunPromptPolicy(skill_injection_mode="append-system-prompt")

    def resolve_launch_spec(
        self,
        run: SpawnParams,
        perms: PermissionResolver,
    ) -> ResolvedLaunchSpec:
        continue_session_id = (run.continue_harness_session_id or "").strip() or None
        return ResolvedLaunchSpec(
            harness=HarnessId.CURSOR,
            model=str(run.model).strip() if run.model else None,
            effort=run.effort,
            prompt=run.prompt,
            continue_session_id=continue_session_id,
            continue_fork=run.continue_fork and continue_session_id is not None,
            permission_resolver=perms,
            extra_args=run.extra_args,
            interactive=run.interactive,
            mcp_tools=run.mcp_tools,
            projected_roots=run.projected_roots,
            task_cwd=run.task_cwd,
        )

    def preflight(
        self,
        *,
        execution_cwd: Path,
        child_cwd: Path,
        passthrough_args: tuple[str, ...],
    ) -> PreflightResult:
        _ = execution_cwd, child_cwd
        if shutil.which(self.BASE_COMMAND[0]) is None:
            raise FileNotFoundError(
                "Cursor CLI not found on PATH. Install Cursor CLI or set PATH to include 'cursor'."
            )
        return PreflightResult.build(expanded_passthrough_args=passthrough_args)

    def build_command(self, run: SpawnParams, perms: PermissionResolver) -> list[str]:
        spec = self.resolve_launch_spec(run, perms)
        return project_subprocess_spec(self.id, spec, base_command=self.BASE_COMMAND)

    def mcp_config(self, run: SpawnParams) -> McpConfig | None:
        _ = run
        return None

    def env_overrides(self, config: PermissionConfig) -> dict[str, str]:
        _ = config
        return {}

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
        return CURSOR_EXTRACTOR.extract_usage(artifacts, spawn_id)

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        return CURSOR_EXTRACTOR.extract_session_id(artifacts, spawn_id)

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        return CURSOR_EXTRACTOR.extract_report(artifacts, spawn_id)


register_harness_bundle(
    HarnessBundle(
        harness_id=HarnessId.CURSOR,
        adapter=CursorAdapter(),
        spec_cls=ResolvedLaunchSpec,
        extractor=CURSOR_EXTRACTOR,
        connections={TransportId.SUBPROCESS: CursorSubprocessConnection},
        projections=HarnessProjectionPorts(
            subprocess_cli_args=project_cursor_spec_to_cli_args,
        ),
    )
)
