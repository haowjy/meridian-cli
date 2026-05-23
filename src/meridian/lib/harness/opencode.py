"""OpenCode CLI harness adapter."""

import logging
import os
import re
import sqlite3
from datetime import datetime
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
    ObserverControllerContract,
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
    ManagedPrimaryProjectionPorts,
    project_subprocess_spec,
    register_harness_bundle,
)
from meridian.lib.harness.common import (
    extract_opencode_report,
    extract_session_id_from_artifacts_with_patterns,
)
from meridian.lib.harness.connections.opencode_http import OpenCodeConnection
from meridian.lib.harness.extractors.opencode import OPENCODE_EXTRACTOR
from meridian.lib.harness.launch_types import SessionSeed
from meridian.lib.harness.opencode_storage import resolve_opencode_session_file
from meridian.lib.harness.projections.project_opencode_streaming import (
    project_opencode_spec_to_serve_command,
    project_opencode_spec_to_session_payload,
)
from meridian.lib.harness.projections.project_opencode_subprocess import (
    project_opencode_spec_to_cli_args,
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
    BASE_COMMAND_OPENCODE_SUBPROCESS,
    PRIMARY_BASE_COMMAND_OPENCODE,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec, TerminalSurfaceMode
from meridian.lib.platform import get_home_path
from meridian.lib.safety.permissions import PermissionConfig

logger = logging.getLogger(__name__)

# Deprecated legacy log parser retained for older OpenCode installs. Modern
# OpenCode stores session metadata in opencode.db and no longer emits this shape.
OPENCODE_SESSION_CREATED_RE = re.compile(
    r"^\w+\s+(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s+\+\d+ms\s+"
    r"service=session\s+id=(?P<session_id>\S+)\s+.*?\bdirectory=(?P<directory>\S+)\b.*\bcreated\b"
)


def _normalize_opencode_model(model: str) -> str:
    """Normalize whitespace in a provider/model identifier.

    OpenCode accepts raw provider/model IDs (e.g. ``opencode-go/kimi-k2.6``).
    Meridian no longer strips harness-routing prefixes; use ``--harness``
    to force routing instead.
    """
    normalized = model.strip()
    provider, separator, model_name = normalized.partition("/")
    if not separator:
        return normalized
    provider = provider.strip()
    model_name = model_name.strip()
    if not provider or not model_name:
        return normalized
    return f"{provider}/{model_name}"


def _opencode_data_root() -> Path:
    xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data_home:
        return Path(xdg_data_home).expanduser()
    return get_home_path() / ".local" / "share"


def _opencode_db_path() -> Path:
    return _opencode_data_root() / "opencode" / "opencode.db"


def _opencode_session_diff_path(session_id: str) -> Path:
    return _opencode_data_root() / "opencode" / "storage" / "session_diff" / f"{session_id}.json"


def _directory_matches_project(directory: str, project_root: Path) -> bool:
    if not directory.strip():
        return False
    try:
        return Path(directory).expanduser().resolve() == project_root.resolve()
    except OSError:
        return False


def _timestamp_to_epoch(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        timestamp = float(value)
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            timestamp = float(raw)
        except ValueError:
            normalized = raw.removesuffix("Z")
            try:
                return datetime.fromisoformat(normalized).timestamp()
            except ValueError:
                return None
    # OpenCode timestamps may be stored as Unix milliseconds.
    if timestamp > 10_000_000_000:
        return timestamp / 1000
    return timestamp


def _query_opencode_sessions(project_root: Path) -> list[tuple[str, float]] | None:
    db_path = _opencode_db_path()
    if not db_path.is_file():
        return None

    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.1) as connection:
            rows = connection.execute(
                """
                SELECT id, directory, time_created, time_updated
                FROM session
                ORDER BY COALESCE(time_updated, time_created) DESC
                """
            ).fetchall()
    except (OSError, sqlite3.Error):
        logger.debug("Failed to query OpenCode session database %s", db_path, exc_info=True)
        return None

    matches: list[tuple[str, float]] = []
    for session_id, directory, time_created, time_updated in rows:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id or not _directory_matches_project(
            str(directory or ""),
            project_root,
        ):
            continue
        updated_at = _timestamp_to_epoch(time_updated)
        created_at = _timestamp_to_epoch(time_created)
        session_epoch = updated_at if updated_at is not None else created_at
        if session_epoch is not None:
            matches.append((normalized_session_id, session_epoch))
    return matches


def _legacy_detect_primary_session_id(
    project_root: Path,
    started_at_epoch: float,
    started_at_local_iso: str,
) -> str | None:
    logs_root = _opencode_data_root() / "opencode" / "log"
    if not logs_root.is_dir():
        return None

    matches: list[tuple[str, str]] = []
    for candidate in logs_root.glob("*.log"):
        try:
            modified_at = candidate.stat().st_mtime
        except OSError:
            continue
        if modified_at + 1 < started_at_epoch:
            continue
        try:
            lines = candidate.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            logger.debug("Failed to read opencode log %s", candidate, exc_info=True)
            continue
        for line in lines:
            match = OPENCODE_SESSION_CREATED_RE.match(line)
            if match is None:
                continue
            timestamp = match.group("ts")
            if timestamp < started_at_local_iso:
                continue
            if not _directory_matches_project(match.group("directory"), project_root):
                continue
            session_id = match.group("session_id").strip()
            if session_id:
                matches.append((timestamp, session_id))

    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1]


def _detect_primary_session_id(
    project_root: Path,
    started_at_epoch: float,
    started_at_local_iso: str,
) -> str | None:
    sessions = _query_opencode_sessions(project_root)
    if sessions is not None:
        recent_sessions = [item for item in sessions if item[1] + 1 >= started_at_epoch]
        if recent_sessions:
            recent_sessions.sort(key=lambda item: item[1], reverse=True)
            return recent_sessions[0][0]
        return None

    diff_root = _opencode_data_root() / "opencode" / "storage" / "session_diff"
    if diff_root.is_dir():
        diff_matches: list[tuple[float, str]] = []
        for candidate in diff_root.glob("ses_*.json"):
            try:
                modified_at = candidate.stat().st_mtime
            except OSError:
                continue
            if modified_at + 1 < started_at_epoch:
                continue
            diff_matches.append((modified_at, candidate.stem))
        if diff_matches:
            diff_matches.sort(key=lambda item: item[0], reverse=True)
            return diff_matches[0][1]

    return _legacy_detect_primary_session_id(project_root, started_at_epoch, started_at_local_iso)


def project_opencode_spec_to_session_payload_for_project(
    spec: ResolvedLaunchSpec,
    *,
    project_root: Path,
) -> dict[str, object]:
    """Project OpenCode managed-primary bootstrap payload for one project root."""

    _ = project_root
    return project_opencode_spec_to_session_payload(spec)


def _legacy_owns_session(project_root: Path, session_ref: str) -> bool:
    opencode_logs = _opencode_data_root() / "opencode" / "log"
    if not opencode_logs.is_dir():
        return False

    for candidate in opencode_logs.glob("*.log"):
        try:
            lines = candidate.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            match = OPENCODE_SESSION_CREATED_RE.match(line)
            if match is None:
                continue
            if match.group("session_id").strip() != session_ref:
                continue
            if _directory_matches_project(match.group("directory"), project_root):
                return True

    return False


def _owns_session(project_root: Path, session_ref: str) -> bool:
    normalized = session_ref.strip()
    if not normalized:
        return False

    db_path = _opencode_db_path()
    if db_path.is_file():
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.1) as connection:
                row = connection.execute(
                    "SELECT directory FROM session WHERE id = ?",
                    (normalized,),
                ).fetchone()
        except (OSError, sqlite3.Error):
            logger.debug("Failed to query OpenCode session database %s", db_path, exc_info=True)
        else:
            if row is not None:
                return _directory_matches_project(str(row[0] or ""), project_root)

    if _opencode_session_diff_path(normalized).is_file():
        return True

    return _legacy_owns_session(project_root, normalized)


class OpenCodeAdapter(BaseHarnessAdapter[ResolvedLaunchSpec]):
    """SubprocessHarness implementation for `opencode`."""

    BASE_COMMAND: ClassVar[tuple[str, ...]] = BASE_COMMAND_OPENCODE_SUBPROCESS
    PRIMARY_BASE_COMMAND: ClassVar[tuple[str, ...]] = PRIMARY_BASE_COMMAND_OPENCODE
    SESSION_ID_KEYS: ClassVar[tuple[str, ...]] = (
        "session_id",
        "sessionId",
        "sessionID",
    )
    SESSION_ID_TEXT_PATTERNS: ClassVar[tuple[re.Pattern[str], ...]] = (
        re.compile(
            r"\bopencode\b[^\n]*?--session(?:=|\s+)([A-Za-z0-9][A-Za-z0-9._:-]{5,})\b",
            re.IGNORECASE,
        ),
    )
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
            "mcp_tools",
            "projected_roots",
            "appended_system_prompt",
            "user_turn_content",
        }
    )
    _EXPLICITLY_IGNORED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "report_output_path",
            "context_from_payload",
            "reference_items",
            "task_cwd",
        }
    )

    @property
    def id(self) -> HarnessId:
        return HarnessId.OPENCODE

    @property
    def contract(self) -> HarnessContract:
        observer = ObserverControllerContract(
            supports_input_injection=False,
        )
        return HarnessContract(
            capabilities=self.capabilities,
            transport=TransportContract(
                transport_ids=(TransportId.STREAMING,),
                observer_controller_required=True,
            ),
            projection=ProjectionContract(
                launch_spec_cls="ResolvedLaunchSpec",
                mode=ProjectionMode.SYSTEM_FIELD_WITH_USER_TURN,
            ),
            extraction=ExtractionContract(
                session_observation_order=(
                    "connection_session",
                    "artifacts",
                    "current_session",
                    "primary_detection",
                )
            ),
            approval=ApprovalContract(
                runtime_hitl=RuntimeHitlMode.NONE,
                subprocess_permission_flags_projected_by_shared_policy=False,
                default_runtime_request_policy="none",
            ),
            bootstrap=BootstrapContract(
                mode=BootstrapMode.MANAGED_PRIMARY_ATTACH,
                fork_materialization=ForkMaterializationMode.NATIVE_CONTINUE_FORK,
                primary_attach_failure_policy="fallback_to_blackbox",
                observer_controller=observer,
            ),
            capability_limits=(
                "native_inherit declared as contract capability only; policy remains pty_mediated",
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
            supports_primary_launch=True,
            supports_native_file_injection=False,
            terminal_surface_modes=(
                TerminalSurfaceMode.PTY_MEDIATED,
                TerminalSurfaceMode.NATIVE_INHERIT,
            ),
            default_terminal_surface_mode=TerminalSurfaceMode.PTY_MEDIATED,
        )

    def run_prompt_policy(self) -> RunPromptPolicy:
        return RunPromptPolicy()

    def resolve_launch_spec(
        self,
        run: SpawnParams,
        perms: PermissionResolver,
    ) -> ResolvedLaunchSpec:
        normalized_model: str | None = None
        if run.model:
            normalized_model = _normalize_opencode_model(str(run.model))
        continue_session_id = (run.continue_harness_session_id or "").strip() or None
        return ResolvedLaunchSpec(
            harness=HarnessId.OPENCODE,
            model=normalized_model,
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
            # OpenCode does not support native meridian agents; agent body is
            # delivered via system prompt composition instead.
            agent_name=None,
            skills=(),
        )

    def build_command(self, run: SpawnParams, perms: PermissionResolver) -> list[str]:
        spec = self.resolve_launch_spec(run, perms)
        base_command = self.PRIMARY_BASE_COMMAND if spec.interactive else self.BASE_COMMAND
        return project_subprocess_spec(self.id, spec, base_command=base_command)

    def mcp_config(self, run: SpawnParams) -> McpConfig | None:
        # MCP injection is off by default — agents use the CLI instead.
        # Users who want always-on MCP can configure it in their harness settings.
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
        overrides: dict[str, str] = {}
        if config.opencode_permission_override:
            overrides["OPENCODE_PERMISSION"] = config.opencode_permission_override
        return overrides

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
        return OPENCODE_EXTRACTOR.extract_usage(artifacts, spawn_id)

    def seed_session(
        self,
        *,
        is_resume: bool,
        harness_session_id: str,
        passthrough_args: tuple[str, ...],
    ) -> SessionSeed:
        _ = is_resume, passthrough_args
        normalized_harness_session_id = harness_session_id.strip()
        if not normalized_harness_session_id:
            return SessionSeed()
        # Resume and fork both seed from an existing harness session id.
        return SessionSeed(session_id=normalized_harness_session_id)

    def detect_primary_session_id(
        self,
        *,
        project_root: Path,
        started_at_epoch: float,
        started_at_local_iso: str | None,
        expected_session_id: str | None = None,
    ) -> str | None:
        _ = expected_session_id
        local_iso = (
            started_at_local_iso
            if started_at_local_iso is not None
            else datetime.fromtimestamp(started_at_epoch).strftime("%Y-%m-%dT%H:%M:%S")
        )
        return _detect_primary_session_id(project_root, started_at_epoch, local_iso)

    def resolve_session_file(self, *, project_root: Path, session_id: str) -> Path | None:
        _ = project_root
        return resolve_opencode_session_file(session_id=session_id)

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        return extract_session_id_from_artifacts_with_patterns(
            artifacts,
            spawn_id,
            json_keys=self.SESSION_ID_KEYS,
            text_patterns=self.SESSION_ID_TEXT_PATTERNS,
        )

    def owns_untracked_session(self, *, project_root: Path, session_ref: str) -> bool:
        return _owns_session(project_root, session_ref)

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        return extract_opencode_report(artifacts, spawn_id)


register_harness_bundle(
    HarnessBundle(
        harness_id=HarnessId.OPENCODE,
        adapter=OpenCodeAdapter(),
        spec_cls=ResolvedLaunchSpec,
        extractor=OPENCODE_EXTRACTOR,
        connections={TransportId.STREAMING: OpenCodeConnection},
        projections=HarnessProjectionPorts(
            subprocess_cli_args=project_opencode_spec_to_cli_args,
            managed_primary=ManagedPrimaryProjectionPorts(
                backend_command=project_opencode_spec_to_serve_command,
                bootstrap_payload=project_opencode_spec_to_session_payload_for_project,
            ),
        ),
    )
)
