"""Cursor CLI harness adapter and bundle registration."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import ClassVar

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.types import HarnessId, TransportId
from meridian.lib.harness.adapter import (
    BaseHarnessAdapter,
    BootstrapMode,
    ForkMaterializationMode,
    HarnessCapabilities,
    HarnessContract,
    McpConfig,
    SpawnParams,
)
from meridian.lib.harness.bundle import (
    HarnessBundle,
    HarnessProjectionPorts,
    register_harness_bundle,
)
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.connections.cursor_subprocess import CursorSubprocessConnection
from meridian.lib.harness.extractors.cursor import CURSOR_EXTRACTOR
from meridian.lib.harness.projections.permissions_cursor import compile_cursor_permissions
from meridian.lib.harness.projections.project_cursor import project_cursor_spec_to_cli_args
from meridian.lib.harness.semantics import (
    MERIDIAN_CONNECTION_CLOSED_EVENT,
    EventSemantics,
    HarnessSemantics,
    TerminalEventOutcome,
    connection_closed_outcome,
    stringify_terminal_error,
)
from meridian.lib.launch.constants import BASE_COMMAND_CURSOR_SUBPROCESS
from meridian.lib.launch.launch_types import (
    PreflightResult,
    ResolvedLaunchSpec,
    TerminalSurfaceMode,
)


class CursorAdapter(BaseHarnessAdapter[ResolvedLaunchSpec]):
    """Subprocess harness implementation for Cursor's ``cursor agent`` CLI."""

    BASE_COMMAND: ClassVar[tuple[str, ...]] = BASE_COMMAND_CURSOR_SUBPROCESS
    _CONSUMED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "prompt",
            "model",
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
            "user_turn_content",
            "reference_items",
            "continue_harness_session_id",
            "continue_fork",
            "effort",
            "model_override_explicit",
            "pi_harness_profile",
        }
    )

    @property
    def id(self) -> HarnessId:
        return HarnessId.CURSOR

    @property
    def contract(self) -> HarnessContract:
        return HarnessContract(
            capabilities=self.capabilities,
            transport_ids=(TransportId.SUBPROCESS,),
            launch_spec_cls="ResolvedLaunchSpec",
            bootstrap_mode=BootstrapMode.SUBPROCESS_ONLY,
            fork_materialization=ForkMaterializationMode.NATIVE_CONTINUE_FORK,
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
            supports_stdin_prompt=False,
            supports_session_resume=True,
            supports_session_fork=False,
            supports_native_agents=False,
            supports_primary_launch=False,
            supports_native_file_injection=False,
            terminal_surface_mode=TerminalSurfaceMode.PTY_MEDIATED,
        )

    def resolve_launch_spec(self, run: SpawnParams) -> ResolvedLaunchSpec:
        continue_session_id = (run.continue_harness_session_id or "").strip() or None
        return ResolvedLaunchSpec(
            harness=HarnessId.CURSOR,
            model=str(run.model).strip() if run.model else None,
            prompt=run.prompt,
            continue_session_id=continue_session_id,
            continue_fork=run.continue_fork and continue_session_id is not None,
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

    def mcp_config(self, run: SpawnParams) -> McpConfig | None:
        _ = run
        return None



def _resolve_cursor_terminal(event: RawHarnessEvent) -> TerminalEventOutcome | None:
    if event.event_type == MERIDIAN_CONNECTION_CLOSED_EVENT:
        return connection_closed_outcome(event)
    if bool(event.payload.get("is_error")):
        error = (
            stringify_terminal_error(event.payload.get("result"))
            or stringify_terminal_error(event.payload.get("error"))
            or "cursor_result_error"
        )
        return TerminalEventOutcome(status=SpawnStatus.FAILED, exit_code=1, error=error)
    subtype = str(event.payload.get("subtype", "")).strip().lower()
    if subtype in {"", "success"}:
        return TerminalEventOutcome(status=SpawnStatus.SUCCEEDED, exit_code=0)
    error = stringify_terminal_error(event.payload.get("result"))
    return TerminalEventOutcome(
        status=SpawnStatus.FAILED,
        exit_code=1,
        error=error or f"cursor_result_{subtype}",
    )


CURSOR_SEMANTICS = HarnessSemantics(
    events={
        "result": EventSemantics(clears_signal=True),
        MERIDIAN_CONNECTION_CLOSED_EVENT: EventSemantics(),
    },
    payload_resolvers={
        "result": _resolve_cursor_terminal,
        MERIDIAN_CONNECTION_CLOSED_EVENT: _resolve_cursor_terminal,
    },
)

register_harness_bundle(
    HarnessBundle(
        harness_id=HarnessId.CURSOR,
        adapter=CursorAdapter(),
        spec_cls=ResolvedLaunchSpec,
        extractor=CURSOR_EXTRACTOR,
        connections={TransportId.SUBPROCESS: CursorSubprocessConnection},
        projections=HarnessProjectionPorts(
            subprocess_cli_args=project_cursor_spec_to_cli_args,
            permission_report=lambda spec: compile_cursor_permissions(spec)[1],
        ),
        semantics=CURSOR_SEMANTICS,
    )
)
