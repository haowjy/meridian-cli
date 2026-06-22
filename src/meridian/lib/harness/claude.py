"""Claude CLI harness adapter."""

import json
import os
from pathlib import Path
from typing import Any, ClassVar, cast
from uuid import uuid4

from meridian.lib.core.conversation import Conversation, ConversationTurn, ToolCall
from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import ArtifactKey, HarnessId, SpawnId, TransportId
from meridian.lib.harness.adapter import (
    CLAUDE_SPAWN_USAGE_VARIANTS,
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
    PrelaunchBootstrapMode,
    ProjectionContract,
    ProjectionMode,
    RecordConfigDirFn,
    RunPromptPolicy,
    SessionSeedMode,
    SpawnParams,
    TransportContract,
)
from meridian.lib.harness.bundle import (
    HarnessBundle,
    HarnessProjectionPorts,
    project_subprocess_spec,
    register_harness_bundle,
)
from meridian.lib.harness.claude_preflight import (
    build_claude_preflight_result,
    ensure_claude_session_accessible,
)
from meridian.lib.harness.claude_sessions import (
    candidate_claude_project_dirs,
    detect_primary_session_id,
    reconcile_tui_trampoline_session_id,
)
from meridian.lib.harness.claude_sessions import (
    project_slug as project_slug,
)
from meridian.lib.harness.claude_utils import (
    extract_session_id_from_args,
    has_session_identity_in_args,
)
from meridian.lib.harness.common import (
    extract_claude_report,
    extract_session_id_from_artifacts_with_patterns,
)
from meridian.lib.harness.connections.claude_ws import ClaudeConnection
from meridian.lib.harness.extractors.claude import CLAUDE_EXTRACTOR
from meridian.lib.harness.launch_types import SessionSeed
from meridian.lib.harness.projections.project_claude import project_claude_spec_to_cli_args
from meridian.lib.launch.claude_session_access import resolve_claude_session_access_source
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
    BASE_COMMAND_CLAUDE_SUBPROCESS,
    OUTPUT_FILENAME,
    PRIMARY_BASE_COMMAND_CLAUDE,
)
from meridian.lib.launch.launch_types import (
    PreflightResult,
    ResolvedLaunchSpec,
    TerminalSurfaceMode,
)
from meridian.lib.launch.request import SessionRequest
from meridian.lib.safety.permissions import PermissionConfig


def build_claude_adhoc_agent_json(
    *,
    name: str,
    description: str,
    prompt: str,
) -> str:
    """Build a Claude `--agents` payload for one installed Meridian agent."""

    normalized_name = name.strip()
    if not normalized_name:
        return ""

    payload = {
        normalized_name: {
            "description": description.strip() or normalized_name,
            "prompt": prompt,
        }
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


_candidate_claude_project_dirs = candidate_claude_project_dirs


def _extract_passthrough_session_id(args: tuple[str, ...]) -> str:
    """Extract --session-id value from passthrough args, or return empty string."""
    for i, token in enumerate(args):
        if token == "--session-id" and i + 1 < len(args):
            return args[i + 1].strip()
        if token.startswith("--session-id="):
            return token.partition("=")[2].strip()
    return ""


def _read_artifact_text(artifacts: ArtifactStore, spawn_id: SpawnId, name: str) -> str:
    key = ArtifactKey(f"{spawn_id}/{name}")
    if not artifacts.exists(key):
        return ""
    return artifacts.get(key).decode("utf-8", errors="ignore")


def _read_output_payloads(artifacts: ArtifactStore, spawn_id: SpawnId) -> list[dict[str, object]]:
    raw_output = _read_artifact_text(artifacts, spawn_id, OUTPUT_FILENAME)
    payloads: list[dict[str, object]] = []
    for line in raw_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload_obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload_obj, dict):
            payloads.append(cast("dict[str, object]", payload_obj))
    return payloads


def _tool_call_from_payload(payload: dict[str, object]) -> ToolCall | None:
    event_type = str(payload.get("type", payload.get("event", ""))).strip().lower()
    if event_type != "tool_use":
        return None

    tool_name = str(payload.get("name", "")).strip()
    if not tool_name:
        return None

    raw_input = payload.get("input")
    tool_input: dict[str, Any] = (
        cast("dict[str, Any]", raw_input) if isinstance(raw_input, dict) else {}
    )
    output_text: str | None = None
    output_value = payload.get("output")
    if isinstance(output_value, str):
        output_text = output_value.strip() or None
    return ToolCall(tool_name=tool_name, input=tool_input, output=output_text)


class ClaudeAdapter(BaseHarnessAdapter[ResolvedLaunchSpec]):
    """SubprocessHarness implementation for `claude`."""

    BASE_COMMAND: ClassVar[tuple[str, ...]] = BASE_COMMAND_CLAUDE_SUBPROCESS
    PRIMARY_BASE_COMMAND: ClassVar[tuple[str, ...]] = PRIMARY_BASE_COMMAND_CLAUDE
    _CONSUMED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "prompt",
            "model",
            "effort",
            "skills",
            "agent",
            "adhoc_agent_payload",
            "extra_args",
            "control_root",
            "interactive",
            "continue_harness_session_id",
            "continue_fork",
            "appended_system_prompt",
            "mcp_tools",
            "projected_roots",
            "user_turn_content",
            "claude_allow_builtin_agents",
        }
    )
    _EXPLICITLY_IGNORED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "context_from_payload",
            "reference_items",
            "task_cwd",
            "pi_harness_profile",
        }
    )

    @property
    def id(self) -> HarnessId:
        return HarnessId.CLAUDE

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
                mode=ProjectionMode.PROMPT_FILE_APPEND_SYSTEM,
            ),
            extraction=ExtractionContract(
                session_observation_order=(
                    "artifacts",
                    "current_session",
                    "primary_detection",
                )
            ),
            approval=ApprovalContract(),
            bootstrap=BootstrapContract(
                mode=BootstrapMode.SUBPROCESS_ONLY,
                fork_materialization=ForkMaterializationMode.NATIVE_CONTINUE_FORK,
                primary_session_seed_mode=SessionSeedMode.PROJECTED_ARGS,
                streaming_session_seed_mode=SessionSeedMode.PROJECTED_ARGS,
                prelaunch_bootstrap_mode=PrelaunchBootstrapMode.ENV_OVERLAY_AND_SESSION_ACCESS,
            ),
            capability_limits=(
                "terminal_surface_mode limited to pty_mediated",
                "no observer/controller backend contract",
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
            supports_native_skills=True,
            supports_native_agents=True,
            supports_primary_launch=True,
            supports_native_file_injection=False,
            terminal_surface_modes=(TerminalSurfaceMode.PTY_MEDIATED,),
            default_terminal_surface_mode=TerminalSurfaceMode.PTY_MEDIATED,
        )

    def run_prompt_policy(self) -> RunPromptPolicy:
        return RunPromptPolicy(
            skill_injection_mode="append-system-prompt",
            spawn_usage_contract_variants=CLAUDE_SPAWN_USAGE_VARIANTS,
        )

    def build_adhoc_agent_payload(self, *, name: str, description: str, prompt: str) -> str:
        return build_claude_adhoc_agent_json(name=name, description=description, prompt=prompt)

    def resolve_launch_spec(
        self, run: SpawnParams, perms: PermissionResolver
    ) -> ResolvedLaunchSpec:
        effort = run.effort
        normalized_effort = None
        if effort is not None:
            normalized_value = str(effort).strip()
            normalized_effort = {
                "low": "low",
                "medium": "medium",
                "high": "high",
                "xhigh": "xhigh",
                "max": "max",
            }.get(normalized_value, normalized_value)
        continue_session_id = (run.continue_harness_session_id or "").strip() or None
        effective_extra_args = run.extra_args
        if continue_session_id is None and not has_session_identity_in_args(run.extra_args):
            effective_extra_args = (*run.extra_args, "--session-id", str(uuid4()))

        # prompt_file_path is owned by bind_launch_context, which sets it to
        # <spawn-log-dir>/system-prompt.md (the single artifact-dir authority).
        prompt_file_path: str | None = None
        # Extract user_turn_content from run params if available
        user_turn_content = getattr(run, "user_turn_content", None)
        disallowed_tools: tuple[str, ...] = ()
        if not run.claude_allow_builtin_agents:
            disallowed_tools = (
                "Agent(Explore),Agent(Plan),Agent(General-purpose),Agent(general-purpose)",
            )
        return ResolvedLaunchSpec(
            harness=HarnessId.CLAUDE,
            model=str(run.model).strip() if run.model else None,
            effort=normalized_effort,
            prompt=run.prompt,
            continue_session_id=continue_session_id,
            continue_fork=run.continue_fork and continue_session_id is not None,
            permission_resolver=perms,
            extra_args=effective_extra_args,
            interactive=run.interactive,
            mcp_tools=run.mcp_tools,
            projected_roots=run.projected_roots,
            appended_system_prompt=run.appended_system_prompt,
            agents_payload=run.adhoc_agent_payload.strip() or None,
            agent_name=run.agent,
            prompt_file_path=prompt_file_path,
            user_turn_content=user_turn_content,
            disallowed_tools=disallowed_tools,
        )

    def preflight(
        self,
        *,
        execution_cwd: Path,
        child_cwd: Path,
        passthrough_args: tuple[str, ...],
    ) -> PreflightResult:
        return build_claude_preflight_result(
            execution_cwd=execution_cwd,
            child_cwd=child_cwd,
            passthrough_args=passthrough_args,
        )

    def build_command(self, run: SpawnParams, perms: PermissionResolver) -> list[str]:
        spec = self.resolve_launch_spec(run, perms)
        base_command = self.PRIMARY_BASE_COMMAND if spec.interactive else self.BASE_COMMAND
        return project_subprocess_spec(self.id, spec, base_command=base_command)

    def mcp_config(self, run: SpawnParams) -> McpConfig | None:
        # MCP injection is off by default — agents use the CLI instead.
        # Users who want always-on MCP can configure it in their harness settings.
        return None

    def env_overrides(self, config: PermissionConfig) -> dict[str, str]:
        _ = config
        return {}

    def blocked_child_env_vars(self) -> frozenset[str]:
        # Meridian manages nesting limits itself; suppress Claude's parent-session
        # sentinel so child Claude spawns can run under Meridian control.
        return frozenset({"CLAUDECODE"})

    def derive_primary_seeded_session_id(
        self,
        *,
        spec: ResolvedLaunchSpec,
        command: tuple[str, ...],
    ) -> str | None:
        return extract_session_id_from_args(command)

    def derive_streaming_seeded_session_id(
        self,
        *,
        spec: ResolvedLaunchSpec,
    ) -> str | None:
        return extract_session_id_from_args(spec.extra_args)

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
        _ = runtime_root, spawn_id, child_env

        configured_root = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
        effective_config_root = (
            Path(configured_root).expanduser().resolve() if configured_root else None
        )
        env_overrides: dict[str, str] = {}
        if effective_config_root is not None:
            effective_config_dir = str(effective_config_root)
            env_overrides["CLAUDE_CONFIG_DIR"] = effective_config_dir
            if record_effective_config_dir is not None:
                record_effective_config_dir(effective_config_dir)

        session_access = resolve_claude_session_access_source(
            session,
            control_root=child_cwd,
            materialization_root=effective_config_root,
            target_config_root=effective_config_root,
        )
        if session_access.should_seed:
            ensure_claude_session_accessible(
                source_session_id=session_access.source_session_id or resolved_harness_session_id,
                source_cwd=session_access.source_control_root,
                child_cwd=session_access.target_control_root or child_cwd,
                source_config_root=session_access.source_config_root,
                target_config_root=session_access.target_config_root,
            )

        return HarnessPrelaunchState(env_overrides=env_overrides)

    def cleanup_prelaunch(
        self,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        chat_id: str | None,
        state: HarnessPrelaunchState,
    ) -> None:
        _ = runtime_root, spawn_id, chat_id, state

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
        return CLAUDE_EXTRACTOR.extract_usage(artifacts, spawn_id)

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        return extract_session_id_from_artifacts_with_patterns(artifacts, spawn_id)

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        return extract_claude_report(artifacts, spawn_id)

    def extract_conversation(
        self, artifacts: ArtifactStore, spawn_id: SpawnId
    ) -> Conversation | None:
        payloads = _read_output_payloads(artifacts, spawn_id)
        tool_calls = tuple(
            tool_call
            for payload in payloads
            if (tool_call := _tool_call_from_payload(payload)) is not None
        )

        # Read user-turn content: prefer starting-prompt.md (new), fall back to prompt.md (legacy)
        prompt_text = (
            _read_artifact_text(artifacts, spawn_id, "starting-prompt.md")
            or _read_artifact_text(artifacts, spawn_id, "prompt.md")
        ).strip()
        report_text = _read_artifact_text(artifacts, spawn_id, "report.md").strip()
        if not report_text:
            fallback_report = extract_claude_report(artifacts, spawn_id)
            report_text = fallback_report.strip() if fallback_report else ""

        if not prompt_text and not report_text and not tool_calls:
            return None

        turns: list[ConversationTurn] = []
        if prompt_text:
            turns.append(ConversationTurn(role="user", content=prompt_text))
        if report_text or tool_calls:
            turns.append(
                ConversationTurn(
                    role="assistant",
                    content=report_text,
                    tool_calls=tool_calls,
                )
            )

        if not turns:
            return None

        return Conversation(
            spawn_id=str(spawn_id),
            harness=str(self.id),
            turns=tuple(turns),
        )

    def seed_session(
        self,
        *,
        is_resume: bool,
        harness_session_id: str,
        passthrough_args: tuple[str, ...],
    ) -> SessionSeed:
        normalized_harness_session_id = harness_session_id.strip()
        # Resume and fork both provide an explicit harness session id. Fork is
        # represented as is_resume=False with harness_session_id set.
        if normalized_harness_session_id:
            return SessionSeed(session_id=normalized_harness_session_id)

        # If user provided --session-id via passthrough, use that value.
        passthrough_session_id = _extract_passthrough_session_id(passthrough_args)
        if passthrough_session_id:
            return SessionSeed(session_id=passthrough_session_id)

        # resolve_launch_spec() seeds --session-id for all launches (interactive
        # and non-interactive).  No seed needed from the session-access layer.
        return SessionSeed()

    def project_content(self, content: ComposedLaunchContent) -> ProjectedContent:
        """Claude projection: route system content to append-system-prompt.

        - SYSTEM_INSTRUCTION (skills, profile, report, inventory, passthrough)
          → --append-system-prompt channel
        - USER_TASK_PROMPT + TASK_CONTEXT → positional prompt argument (user turn)
        """
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
                system_instruction=("append-system-prompt" if system_prompt.strip() else "none"),
                user_task_prompt="user-turn",
                task_context="user-turn",
            ),
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
        return detect_primary_session_id(
            project_root,
            started_at_epoch,
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
        _ = started_at_local_iso

        live_session_id = (connection_session_id or "").strip()
        if live_session_id:
            return live_session_id

        if spawn_id is not None:
            extracted_session_id = (self.extract_session_id(artifacts, spawn_id) or "").strip()
            if extracted_session_id:
                return extracted_session_id

        normalized_current = (current_session_id or expected_session_id or "").strip()
        if not normalized_current:
            return None
        if project_root is None:
            return normalized_current

        reconciled = reconcile_tui_trampoline_session_id(
            project_root=project_root,
            recorded_session_id=normalized_current,
            started_at_epoch=started_at_epoch,
        )
        return reconciled or normalized_current

    def resolve_session_file(self, *, project_root: Path, session_id: str) -> Path | None:
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            return None
        for project_dir in _candidate_claude_project_dirs(project_root):
            candidate = project_dir / f"{normalized_session_id}.jsonl"
            if candidate.is_file():
                return candidate
        return None

    def owns_untracked_session(self, *, project_root: Path, session_ref: str) -> bool:
        normalized_session_ref = session_ref.strip()
        if not normalized_session_ref:
            return False
        for project_dir in _candidate_claude_project_dirs(project_root):
            session_file = project_dir / f"{normalized_session_ref}.jsonl"
            if session_file.is_file():
                return True
        return False


register_harness_bundle(
    HarnessBundle(
        harness_id=HarnessId.CLAUDE,
        adapter=ClaudeAdapter(),
        spec_cls=ResolvedLaunchSpec,
        extractor=CLAUDE_EXTRACTOR,
        connections={TransportId.STREAMING: ClaudeConnection},
        projections=HarnessProjectionPorts(
            subprocess_cli_args=project_claude_spec_to_cli_args,
        ),
    )
)
