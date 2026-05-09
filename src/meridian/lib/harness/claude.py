"""Claude CLI harness adapter."""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, ClassVar, cast
from uuid import uuid4

from meridian.lib.core.conversation import Conversation, ConversationTurn, ToolCall
from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import ArtifactKey, SpawnId
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
from meridian.lib.harness.ids import HarnessId, TransportId
from meridian.lib.harness.launch_spec import ClaudeLaunchSpec
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
from meridian.lib.platform import get_home_path
from meridian.lib.safety.permissions import PermissionConfig

logger = logging.getLogger(__name__)


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


def project_slug(project_root: Path) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", str(project_root.resolve()))


def _claude_config_root() -> Path:
    configured_root = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if configured_root:
        return Path(configured_root).expanduser()
    return get_home_path() / ".claude"


def _claude_projects_root() -> Path:
    return _claude_config_root() / "projects"


def _claude_project_dir(project_root: Path) -> Path:
    return _claude_projects_root() / project_slug(project_root)


def _candidate_claude_project_dirs(project_root: Path) -> list[Path]:
    """Return the exact Claude project directory for this project root."""
    return [_claude_projects_root() / project_slug(project_root)]


def _read_claude_session_id(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            first_line = handle.readline().strip()
    except OSError:
        logger.debug("Failed to read Claude session file %s", path, exc_info=True)
        return None
    if not first_line:
        return None
    try:
        payload = json.loads(first_line)
    except json.JSONDecodeError:
        return path.stem.strip() or None
    if not isinstance(payload, dict):
        return path.stem.strip() or None
    payload_dict = cast("dict[str, object]", payload)
    session_id = payload_dict.get("sessionId")
    if isinstance(session_id, str) and session_id.strip():
        return session_id.strip()
    return path.stem.strip() or None


def _detect_primary_session_id(
    project_root: Path,
    started_at_epoch: float,
    *,
    expected_session_id: str | None = None,
) -> str | None:
    """Detect Claude primary session ID by verifying a known session file only."""
    if not expected_session_id:
        logger.debug("No expected session ID for primary detection; skipping heuristic scan")
        return None

    project_dir = _claude_project_dir(project_root)
    if not project_dir.is_dir():
        logger.warning(
            "Expected Claude session directory not found",
            extra={"session_id": expected_session_id, "project_dir": str(project_dir)},
        )
        return None

    candidate = project_dir / f"{expected_session_id}.jsonl"
    try:
        if not candidate.is_file():
            logger.warning(
                "Expected Claude session file not found",
                extra={"session_id": expected_session_id, "project_dir": str(project_dir)},
            )
            return None
        if candidate.stat().st_mtime + 1 < started_at_epoch:
            return None
        resolved = _read_claude_session_id(candidate)
        if resolved == expected_session_id:
            return expected_session_id
        logger.warning(
            "Claude session file exists but embedded ID mismatches",
            extra={"expected": expected_session_id, "found": resolved},
        )
    except OSError:
        logger.debug("Failed to verify Claude session file", exc_info=True)
    return None


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


class ClaudeAdapter(BaseHarnessAdapter[ClaudeLaunchSpec]):
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
            "project_root",
            "interactive",
            "continue_harness_session_id",
            "continue_fork",
            "appended_system_prompt",
            "mcp_tools",
            "projected_roots",
            "user_turn_content",
        }
    )
    _EXPLICITLY_IGNORED_FIELDS: ClassVar[frozenset[str]] = frozenset({"report_output_path"})

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
                launch_spec_cls="ClaudeLaunchSpec",
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
        return RunPromptPolicy(skill_injection_mode="append-system-prompt")

    def build_adhoc_agent_payload(self, *, name: str, description: str, prompt: str) -> str:
        return build_claude_adhoc_agent_json(name=name, description=description, prompt=prompt)

    def resolve_launch_spec(self, run: SpawnParams, perms: PermissionResolver) -> ClaudeLaunchSpec:
        effort = run.effort
        normalized_effort = None
        if effort is not None:
            normalized_value = str(effort).strip()
            normalized_effort = {
                "low": "low",
                "medium": "medium",
                "high": "high",
                "xhigh": "max",
            }.get(normalized_value, normalized_value)
        continue_session_id = (run.continue_harness_session_id or "").strip() or None
        effective_extra_args = run.extra_args
        if (
            not run.interactive
            and continue_session_id is None
            and not has_session_identity_in_args(run.extra_args)
        ):
            effective_extra_args = (*run.extra_args, "--session-id", str(uuid4()))

        # Prefer the spawn log directory (from report_output_path) for system-prompt.md.
        # Keep project_root fallback for compatibility with contexts that do not set
        # report_output_path.
        prompt_file_path: str | None = None
        report_output_path = (run.report_output_path or "").strip()
        if report_output_path:
            prompt_file_path = str(
                Path(report_output_path).expanduser().parent / "system-prompt.md"
            )
        elif run.project_root:
            prompt_file_path = str(Path(run.project_root) / "system-prompt.md")
        # Extract user_turn_content from run params if available
        user_turn_content = getattr(run, "user_turn_content", None)
        return ClaudeLaunchSpec(
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
        if not isinstance(spec, ClaudeLaunchSpec):
            return None
        return extract_session_id_from_args(command)

    def derive_streaming_seeded_session_id(
        self,
        *,
        spec: ResolvedLaunchSpec,
    ) -> str | None:
        if not isinstance(spec, ClaudeLaunchSpec):
            return None
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
            child_cwd=child_cwd,
            materialization_root=effective_config_root,
            target_config_root=effective_config_root,
        )
        if session_access.should_seed:
            ensure_claude_session_accessible(
                source_session_id=session_access.source_session_id or resolved_harness_session_id,
                source_cwd=session_access.source_cwd,
                child_cwd=child_cwd,
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

        # Claude rejects --session-id for fresh interactive primary launches.
        # Child/non-interactive runs still get explicit IDs via resolve_launch_spec().
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
                system_instruction=(
                    "append-system-prompt" if system_prompt.strip() else "none"
                ),
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
        return _detect_primary_session_id(
            project_root,
            started_at_epoch,
            expected_session_id=expected_session_id,
        )

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
        spec_cls=ClaudeLaunchSpec,
        extractor=CLAUDE_EXTRACTOR,
        connections={TransportId.STREAMING: ClaudeConnection},
        projections=HarnessProjectionPorts(
            subprocess_cli_args=project_claude_spec_to_cli_args,
        ),
    )
)
