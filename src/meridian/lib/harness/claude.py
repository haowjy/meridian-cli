"""Claude CLI harness adapter."""

import json
import secrets
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from meridian.lib.config.loader import load_launch_config
from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.native_identity import (
    LaunchIntent,
    NativeIdentity,
    NativeKeyFields,
    NativeObservationBatch,
    NativeSessionUnavailable,
    Operation,
    PostExit,
)
from meridian.lib.core.types import HarnessId, SpawnId, TransportId
from meridian.lib.harness.adapter import (
    CLAUDE_SPAWN_USAGE_VARIANTS,
    BaseHarnessAdapter,
    BootstrapMode,
    ForkMaterializationMode,
    HarnessCapabilities,
    HarnessContract,
    HarnessPrelaunchState,
    McpConfig,
    PermissionResolver,
    RecordConfigDirFn,
    RunPromptPolicy,
    SpawnParams,
)
from meridian.lib.harness.bundle import (
    HarnessBundle,
    HarnessProjectionPorts,
    register_harness_bundle,
)
from meridian.lib.harness.claude_boundary import (
    claude_boundary_journal_path,
    read_observations,
)
from meridian.lib.harness.claude_idle import detect_ttl
from meridian.lib.harness.claude_inbox import post_primary_message
from meridian.lib.harness.claude_preflight import (
    build_claude_preflight_result,
    ensure_claude_session_accessible,
)
from meridian.lib.harness.claude_runtime_resolver import resolve_claude_runtime
from meridian.lib.harness.claude_sessions import (
    candidate_claude_project_dirs,
    reconcile_tui_trampoline_session_id,
    resolve_claude_config_root,
    resolve_claude_native_session_file,
)
from meridian.lib.harness.claude_sessions import (
    project_slug as project_slug,
)
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.connections.claude_ws import ClaudeConnection
from meridian.lib.harness.extractors.claude import CLAUDE_EXTRACTOR
from meridian.lib.harness.launch_types import SessionSeed
from meridian.lib.harness.projections.project_claude import project_claude_spec_to_cli_args
from meridian.lib.harness.semantics import (
    MERIDIAN_CONNECTION_CLOSED_EVENT,
    EventSemantics,
    HarnessSemantics,
    PrimaryEventScope,
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
    BASE_COMMAND_CLAUDE_SUBPROCESS,
    PRIMARY_BASE_COMMAND_CLAUDE,
)
from meridian.lib.launch.launch_types import (
    CompositionWarning,
    PreflightResult,
    ResolvedLaunchSpec,
    TerminalSurfaceMode,
)
from meridian.lib.launch.request import SessionRequest
from meridian.lib.safety.permissions import PermissionConfig

_BOUNDARY_PLUGIN = "meridian-session-boundary"
_IDLE_PLUGIN = "meridian-idle"
_SPAWN_WATCH_PLUGIN = "meridian-spawn-watch"


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


class ClaudeAdapter(BaseHarnessAdapter[ResolvedLaunchSpec]):
    """SubprocessHarness implementation for `claude`."""

    native_identity = True
    refused_identity_flags = frozenset(
        ["--session-id", "--resume", "--continue", "--fork-session", "-r", "-c"]
    )
    resolves_untracked_source = True

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
            "allow_builtin_agents",
        }
    )
    _EXPLICITLY_IGNORED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "model_override_explicit",
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
            transport_ids=(TransportId.STREAMING,),
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
            supports_stdin_prompt=True,
            supports_session_resume=True,
            supports_session_fork=True,
            supports_native_agents=True,
            supports_primary_launch=True,
            supports_named_primary_resume=True,
            supports_native_file_injection=False,
            captures_blackbox_output=True,
            terminal_surface_mode=TerminalSurfaceMode.PTY_MEDIATED,
        )

    def run_prompt_policy(self) -> RunPromptPolicy:
        return RunPromptPolicy(
            spawn_usage_contract_variants=CLAUDE_SPAWN_USAGE_VARIANTS,
        )

    def build_adhoc_agent_payload(self, *, name: str, description: str, prompt: str) -> str:
        return build_claude_adhoc_agent_json(name=name, description=description, prompt=prompt)

    def native_store_for_launch(
        self,
        *,
        child_env: Mapping[str, str],
        child_cwd: Path,
        spawn_id: SpawnId,
        operation: Operation,
        interactive: bool,
    ) -> str:
        return str(
            (
                resolve_claude_config_root(child_env, child_cwd)
                / "projects"
                / project_slug(child_cwd)
            ).resolve()
        )

    def assign_session_id(self, intent: LaunchIntent, *, store: Path) -> str | None:
        return (
            str(uuid4())
            if intent.operation == "create"
            else super().assign_session_id(intent, store=store)
        )

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

        # prompt_file_path is owned by bind_launch_context, which sets it to
        # <spawn-log-dir>/system-prompt.md (the single artifact-dir authority).
        prompt_file_path: str | None = None
        # Extract user_turn_content from run params if available
        user_turn_content = getattr(run, "user_turn_content", None)
        disallowed_tools: tuple[str, ...] = ()
        if not run.allow_builtin_agents:
            disallowed_tools = (
                "Agent(Explore),Agent(Plan),Agent(General-purpose),Agent(general-purpose)",
            )
        plugin_names = (_BOUNDARY_PLUGIN,)
        if run.interactive:
            plugin_names += (_IDLE_PLUGIN,)
            if load_launch_config(run.control_root).harness.claude.spawn_watch.enabled:
                plugin_names += (_SPAWN_WATCH_PLUGIN,)
        return ResolvedLaunchSpec(
            harness=HarnessId.CLAUDE,
            model=str(run.model).strip() if run.model else None,
            effort=normalized_effort,
            prompt=run.prompt,
            continue_session_id=continue_session_id,
            continue_fork=run.continue_fork and continue_session_id is not None,
            permission_resolver=perms,
            extra_args=run.extra_args,
            interactive=run.interactive,
            mcp_tools=run.mcp_tools,
            projected_roots=run.projected_roots,
            appended_system_prompt=run.appended_system_prompt,
            agents_payload=run.adhoc_agent_payload.strip() or None,
            agent_name=run.agent,
            prompt_file_path=prompt_file_path,
            user_turn_content=user_turn_content,
            disallowed_tools=disallowed_tools,
            claude_runtime=resolve_claude_runtime(plugin_names=plugin_names),
        )

    def launch_spec_warnings(
        self, spec: ResolvedLaunchSpec
    ) -> tuple[CompositionWarning, ...]:
        return spec.claude_runtime.warnings

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
        effective_config_root = resolve_claude_config_root(child_env, child_cwd)
        if record_effective_config_dir is not None:
            record_effective_config_dir(str(effective_config_root))

        source_id = session.requested_harness_session_id
        if source_id:
            source_store = session.source_native_store
            ensure_claude_session_accessible(
                source_session_id=source_id,
                child_cwd=child_cwd,
                source_native_store=Path(source_store)
                if source_store
                else (effective_config_root / "projects" / project_slug(child_cwd)),
                target_config_root=effective_config_root,
            )

        return HarnessPrelaunchState(env_overrides={
            "_MERIDIAN_CLAUDE_SESSION_BOUNDARY_PATH": str(
                claude_boundary_journal_path(runtime_root, spawn_id)
            ),
            "_MERIDIAN_CLAUDE_SESSION_BOUNDARY_NONCE": secrets.token_hex(32),
            "_MERIDIAN_CLAUDE_SESSION_BOUNDARY_PYTHON": sys.executable,
        })

    def cleanup_prelaunch(
        self,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        chat_id: str | None,
        state: HarnessPrelaunchState,
    ) -> None:
        _ = runtime_root, spawn_id, chat_id, state

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
        _ = child_env, pid
        successor = reconcile_tui_trampoline_session_id(
            project_root=child_cwd,
            recorded_session_id=entry.session_id or "",
            started_at_epoch=started_at_epoch,
            native_store=Path(identity.native_store),
        )
        return PostExit(
            trampoline_successor_id=successor if successor != entry.session_id else None,
        )

    def native_observation_reader(
        self,
        *,
        child_env: Mapping[str, str],
        pid: int,
    ) -> Callable[[int], NativeObservationBatch] | None:
        raw_path = child_env.get("_MERIDIAN_CLAUDE_SESSION_BOUNDARY_PATH")
        nonce = child_env.get("_MERIDIAN_CLAUDE_SESSION_BOUNDARY_NONCE")
        if not raw_path or not nonce:
            return None
        path = Path(raw_path)
        return lambda cursor: read_observations(path, nonce=nonce, pid=pid, cursor=cursor)

    def resolve_native_session_file(
        self,
        *,
        session_id: str,
        native_store: Path,
    ) -> Path | None:
        return resolve_claude_native_session_file(
            session_id=session_id,
            native_store=native_store,
        )

    def resolve_session_file(
        self,
        *,
        project_root: Path,
        session_id: str,
        config_root_hint: Path | None = None,
    ) -> Path | None:
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            return None
        project_dirs = _candidate_claude_project_dirs(project_root, config_root_hint)
        matches = [
            directory / f"{normalized_session_id}.jsonl"
            for directory in project_dirs
            if (directory / f"{normalized_session_id}.jsonl").is_file()
        ]
        if len(matches) > 1:
            raise NativeSessionUnavailable(normalized_session_id, "ambiguous_native_file")
        if not matches:
            return None
        return matches[0]

    def owns_untracked_session(self, *, project_root: Path, session_ref: str) -> bool:
        normalized_session_ref = session_ref.strip()
        if not normalized_session_ref:
            return False
        for project_dir in _candidate_claude_project_dirs(project_root):
            session_file = project_dir / f"{normalized_session_ref}.jsonl"
            if session_file.is_file():
                return True
        return False


def _resolve_claude_terminal(event: RawHarnessEvent) -> TerminalEventOutcome | None:
    if event.event_type == MERIDIAN_CONNECTION_CLOSED_EVENT:
        return connection_closed_outcome(event)
    if bool(event.payload.get("is_error")):
        error = (
            stringify_terminal_error(event.payload.get("result"))
            or stringify_terminal_error(event.payload.get("error"))
            or "claude_result_error"
        )
        return TerminalEventOutcome(status=SpawnStatus.FAILED, exit_code=1, error=error)

    subtype = str(event.payload.get("subtype", "")).strip().lower()
    terminal_reason = str(event.payload.get("terminal_reason", "")).strip().lower()
    if subtype in {"", "success"} and terminal_reason in {"", "completed"}:
        return TerminalEventOutcome(status=SpawnStatus.SUCCEEDED, exit_code=0)
    if terminal_reason == "completed":
        return TerminalEventOutcome(status=SpawnStatus.SUCCEEDED, exit_code=0)

    error = stringify_terminal_error(event.payload.get("result"))
    if subtype not in {"", "success"}:
        error = error or f"claude_result_{subtype}"
    elif terminal_reason:
        error = error or f"claude_terminal_{terminal_reason}"
    else:
        error = error or "claude_result_unknown"
    return TerminalEventOutcome(status=SpawnStatus.FAILED, exit_code=1, error=error)


class _ClaudeSemantics(HarnessSemantics):
    def normalize(
        self,
        event: RawHarnessEvent,
        *,
        primary_event_scope: PrimaryEventScope | None = None,
    ) -> EventSemantics:
        # Background Agent frames share stdout, but cannot reset the parent's wait.
        if event.event_type == "assistant" and event.payload.get("parent_tool_use_id"):
            return EventSemantics()
        return super().normalize(event, primary_event_scope=primary_event_scope)


CLAUDE_SEMANTICS = _ClaudeSemantics(
    events={
        # Load-bearing: clears awaiting-done and resets the resident nudge timer.
        "assistant": EventSemantics(activity="turn_active"),
        "result": EventSemantics(clears_signal=True),
        MERIDIAN_CONNECTION_CLOSED_EVENT: EventSemantics(),
    },
    payload_resolvers={
        "result": _resolve_claude_terminal,
        MERIDIAN_CONNECTION_CLOSED_EVENT: _resolve_claude_terminal,
    },
)

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
        semantics=CLAUDE_SEMANTICS,
        primary_inbox=post_primary_message,
        detect_ttl=detect_ttl,
    )
)
