"""Codex CLI harness adapter."""

import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import ClassVar, cast
from uuid import uuid4

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
from meridian.lib.harness.codex_rollout import (
    CODEX_ROLLOUT_FILENAME_RE,
    resolve_rollout_session_id,
)
from meridian.lib.harness.common import (
    extract_codex_report,
    extract_session_id_from_artifacts_with_patterns,
)
from meridian.lib.harness.connections.base import (
    PrimaryRuntimeEventSurface,
    PrimaryRuntimeRequestPolicy,
)
from meridian.lib.harness.connections.codex_ws import CodexConnection
from meridian.lib.harness.extractors.codex import CODEX_EXTRACTOR
from meridian.lib.harness.projections.project_codex_streaming import (
    project_codex_spec_to_appserver_command,
    project_codex_spec_to_thread_request,
)
from meridian.lib.harness.projections.project_codex_subprocess import (
    project_codex_spec_to_cli_args,
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
    BASE_COMMAND_CODEX_SUBPROCESS,
    PRIMARY_BASE_COMMAND_CODEX,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec, TerminalSurfaceMode
from meridian.lib.platform import get_home_path
from meridian.lib.safety.permissions import PermissionConfig

logger = logging.getLogger(__name__)


def _codex_home() -> Path:
    configured_home = os.environ.get("CODEX_HOME", "").strip()
    if configured_home:
        return Path(configured_home).expanduser()
    return get_home_path() / ".codex"


def _fsync_directory(path: Path) -> None:
    if sys.platform == "win32":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _rewrite_forked_session_meta(line: str, new_session_id: str) -> str:
    try:
        payload_obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Codex rollout first line is not valid JSON.") from exc
    if not isinstance(payload_obj, dict):
        raise RuntimeError("Codex rollout first line must be a JSON object.")
    payload_dict_obj = cast("dict[str, object]", payload_obj)
    payload = payload_dict_obj.get("payload")
    if payload_dict_obj.get("type") != "session_meta" or not isinstance(payload, dict):
        raise RuntimeError("Codex rollout first line must be a session_meta payload.")

    payload_dict = cast("dict[str, object]", payload)
    payload_dict["id"] = new_session_id
    payload_dict_obj["payload"] = payload_dict
    return json.dumps(payload_dict_obj) + "\n"


def _copy_rollout_atomic(*, source_path: Path, target_path: Path, new_session_id: str) -> None:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.",
        suffix=".tmp",
        dir=target_path.parent,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target_handle:
            with source_path.open("r", encoding="utf-8") as source_handle:
                first_line = source_handle.readline()
                if not first_line:
                    raise RuntimeError(f"Codex rollout file is empty: {source_path}")
                target_handle.write(_rewrite_forked_session_meta(first_line, new_session_id))
                for line in source_handle:
                    target_handle.write(line)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        os.replace(tmp_path, target_path)
        _fsync_directory(target_path.parent)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _fork_rollout_path(*, source_path: Path, source_session_id: str, new_session_id: str) -> Path:
    source_name = source_path.name
    if source_session_id in source_name:
        return source_path.with_name(source_name.replace(source_session_id, new_session_id, 1))
    return source_path.with_name(f"{source_path.stem}-{new_session_id}{source_path.suffix}")


def _resolve_rollout_session_id(path: Path, resolved_repo: Path) -> str | None:
    return resolve_rollout_session_id(path, resolved_repo)


def project_codex_spec_to_thread_request_for_project(
    spec: ResolvedLaunchSpec,
    *,
    project_root: Path,
) -> tuple[str, dict[str, object]]:
    """Project Codex managed-primary bootstrap payload for one project root."""

    return project_codex_spec_to_thread_request(spec, cwd=str(project_root))


def _detect_primary_session_id(project_root: Path, started_at_epoch: float) -> str | None:
    sessions_root = _codex_home() / "sessions"
    if not sessions_root.is_dir():
        return None

    resolved_repo = project_root.resolve()
    candidates: list[tuple[float, Path]] = []
    for candidate in sessions_root.rglob("rollout-*.jsonl"):
        if CODEX_ROLLOUT_FILENAME_RE.match(candidate.name) is None:
            continue
        try:
            modified_at = candidate.stat().st_mtime
        except OSError:
            continue
        if modified_at + 1 < started_at_epoch:
            continue
        candidates.append((modified_at, candidate))

    for _, path in sorted(candidates, key=lambda item: item[0], reverse=True):
        try:
            resolved = _resolve_rollout_session_id(path, resolved_repo)
        except OSError:
            logger.debug("Failed to read codex rollout %s", path, exc_info=True)
            continue
        if resolved is not None:
            return resolved
    return None


def _owns_session(project_root: Path, session_ref: str) -> bool:
    normalized = session_ref.strip()
    if not normalized:
        return False

    resolved_repo = project_root.resolve()
    codex_root = _codex_home() / "sessions"
    if not codex_root.is_dir():
        return False

    for candidate in codex_root.rglob(f"rollout-*-{normalized}.jsonl"):
        if CODEX_ROLLOUT_FILENAME_RE.match(candidate.name) is None:
            continue
        try:
            with candidate.open("r", encoding="utf-8", errors="ignore") as handle:
                for _ in range(5):
                    line = handle.readline()
                    if not line:
                        break
                    raw_payload_obj = json.loads(line)
                    if not isinstance(raw_payload_obj, dict):
                        continue
                    payload_obj = cast("dict[str, object]", raw_payload_obj)
                    if payload_obj.get("type") != "session_meta":
                        continue
                    raw_payload = payload_obj.get("payload")
                    if not isinstance(raw_payload, dict):
                        continue
                    payload = cast("dict[str, object]", raw_payload)
                    session_id = payload.get("id")
                    cwd = payload.get("cwd")
                    if not isinstance(session_id, str) or session_id.strip() != normalized:
                        continue
                    if not isinstance(cwd, str):
                        continue
                    try:
                        cwd_matches = Path(cwd).expanduser().resolve() == resolved_repo
                    except OSError:
                        continue
                    if cwd_matches:
                        return True
        except (OSError, json.JSONDecodeError):
            continue

    return False


class CodexAdapter(BaseHarnessAdapter[ResolvedLaunchSpec]):
    """SubprocessHarness implementation for `codex`."""

    BASE_COMMAND: ClassVar[tuple[str, ...]] = BASE_COMMAND_CODEX_SUBPROCESS
    PRIMARY_BASE_COMMAND: ClassVar[tuple[str, ...]] = PRIMARY_BASE_COMMAND_CODEX
    SESSION_ID_KEYS: ClassVar[tuple[str, ...]] = (
        "session_id",
        "sessionId",
        "sessionID",
        "conversation_id",
        "conversationId",
        "thread_id",
        "threadId",
    )
    SESSION_ID_TEXT_PATTERNS: ClassVar[tuple[re.Pattern[str], ...]] = (
        re.compile(r"\bcodex\s+resume\s+([A-Za-z0-9][A-Za-z0-9._:-]{5,})\b", re.IGNORECASE),
        re.compile(r"\bresume\s+([A-Za-z0-9][A-Za-z0-9._:-]{5,})\b", re.IGNORECASE),
    )
    _CONSUMED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "model",
            "effort",
            "prompt",
            "continue_harness_session_id",
            "continue_fork",
            "extra_args",
            "interactive",
            "mcp_tools",
            "projected_roots",
            "report_output_path",
            "control_root",
            "adhoc_agent_payload",
            "appended_system_prompt",
            "user_turn_content",
        }
    )
    _EXPLICITLY_IGNORED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "skills",
            "agent",
            "context_from_payload",
            "reference_items",
            "task_cwd",
            "pi_harness_profile",
            "claude_allow_builtin_agents",
        }
    )

    @property
    def id(self) -> HarnessId:
        return HarnessId.CODEX

    @property
    def contract(self) -> HarnessContract:
        observer = ObserverControllerContract()
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
                runtime_hitl=RuntimeHitlMode.CONNECTION_REQUESTS,
                default_runtime_request_policy="auto_accept",
                primary_session_runtime_request_policy=(PrimaryRuntimeRequestPolicy.SURFACE_EVENTS),
                primary_session_runtime_event_surface=(
                    PrimaryRuntimeEventSurface.CONNECTION_EVENT_STREAM
                ),
            ),
            bootstrap=BootstrapContract(
                mode=BootstrapMode.MANAGED_PRIMARY_ATTACH,
                fork_materialization=ForkMaterializationMode.MERIDIAN_MATERIALIZED_FORK,
                primary_attach_failure_policy="raise",
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
            supports_native_agents=True,
            supports_primary_launch=True,
            requires_initial_prompt=True,
            supports_native_file_injection=False,
            terminal_surface_modes=(
                TerminalSurfaceMode.PTY_MEDIATED,
                TerminalSurfaceMode.NATIVE_INHERIT,
            ),
            default_terminal_surface_mode=TerminalSurfaceMode.PTY_MEDIATED,
        )

    def run_prompt_policy(self) -> RunPromptPolicy:
        return RunPromptPolicy()

    def build_adhoc_agent_payload(self, *, name: str, description: str, prompt: str) -> str:
        _ = name, description
        return prompt.strip()

    def resolve_launch_spec(
        self, run: SpawnParams, perms: PermissionResolver
    ) -> ResolvedLaunchSpec:
        continue_session_id = (run.continue_harness_session_id or "").strip() or None
        return ResolvedLaunchSpec(
            harness=HarnessId.CODEX,
            model=str(run.model).strip() if run.model else None,
            effort=run.effort,
            prompt=run.user_turn_content or run.prompt,
            continue_session_id=continue_session_id,
            continue_fork=run.continue_fork and continue_session_id is not None,
            permission_resolver=perms,
            extra_args=run.extra_args,
            report_output_path=run.report_output_path,
            interactive=run.interactive,
            mcp_tools=run.mcp_tools,
            projected_roots=run.projected_roots,
            base_instructions=run.adhoc_agent_payload.strip() or None,
            developer_instructions=run.appended_system_prompt,
            user_turn_content=run.user_turn_content,
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
        _ = config
        return {}

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
        return CODEX_EXTRACTOR.extract_usage(artifacts, spawn_id)

    def detect_primary_session_id(
        self,
        *,
        project_root: Path,
        started_at_epoch: float,
        started_at_local_iso: str | None,
        expected_session_id: str | None = None,
    ) -> str | None:
        _ = started_at_local_iso, expected_session_id
        return _detect_primary_session_id(project_root, started_at_epoch)

    def resolve_session_file(self, *, project_root: Path, session_id: str) -> Path | None:
        _ = project_root
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            return None

        sessions_root = _codex_home() / "sessions"
        if not sessions_root.is_dir():
            return None

        matches: list[tuple[float, Path]] = []
        for candidate in sessions_root.rglob(f"rollout-*-{normalized_session_id}.jsonl"):
            if CODEX_ROLLOUT_FILENAME_RE.match(candidate.name) is None:
                continue
            try:
                modified_at = candidate.stat().st_mtime
            except OSError:
                continue
            matches.append((modified_at, candidate))

        if not matches:
            return None

        matches.sort(key=lambda item: item[0], reverse=True)
        return matches[0][1]

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        return extract_session_id_from_artifacts_with_patterns(
            artifacts,
            spawn_id,
            json_keys=self.SESSION_ID_KEYS,
            text_patterns=self.SESSION_ID_TEXT_PATTERNS,
        )

    def fork_session(self, source_session_id: str) -> str:
        normalized_source_session_id = source_session_id.strip()
        if not normalized_source_session_id:
            raise ValueError("source_session_id is required.")

        db_path = _codex_home() / "state_5.sqlite"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(str(db_path), timeout=10)
            connection.row_factory = sqlite3.Row
            source_row = connection.execute(
                "SELECT * FROM threads WHERE id = ?",
                (normalized_source_session_id,),
            ).fetchone()
            if source_row is None:
                raise ValueError(
                    f"Codex session '{normalized_source_session_id}' not found in threads table."
                )

            source_rollout_path_raw = source_row["rollout_path"]
            if not isinstance(source_rollout_path_raw, str):
                raise RuntimeError("Codex threads.rollout_path must be a string.")

            source_rollout_path = Path(source_rollout_path_raw).expanduser()
            if not source_rollout_path.is_file():
                raise FileNotFoundError(f"Codex rollout file not found: {source_rollout_path}")

            new_session_id = str(uuid4())
            target_rollout_path = _fork_rollout_path(
                source_path=source_rollout_path,
                source_session_id=normalized_source_session_id,
                new_session_id=new_session_id,
            )
            _copy_rollout_atomic(
                source_path=source_rollout_path,
                target_path=target_rollout_path,
                new_session_id=new_session_id,
            )

            # Codex owns this schema. Clone source row and only patch Meridian-relevant
            # fields to reduce coupling to Codex-internal table evolution.
            inserted_values = dict(source_row)
            inserted_values["id"] = new_session_id
            inserted_values["rollout_path"] = str(target_rollout_path)
            now = int(time.time())
            for timestamp_column in ("created_at", "updated_at"):
                if timestamp_column in inserted_values and isinstance(
                    inserted_values[timestamp_column], int
                ):
                    inserted_values[timestamp_column] = now

            columns = tuple(inserted_values.keys())
            column_sql = ", ".join(f'"{column_name}"' for column_name in columns)
            placeholders = ", ".join("?" for _ in columns)
            connection.execute(
                f"INSERT INTO threads ({column_sql}) VALUES ({placeholders})",
                tuple(inserted_values[column_name] for column_name in columns),
            )
            connection.commit()
            return new_session_id
        except (FileNotFoundError, ValueError):
            raise
        except (OSError, RuntimeError, json.JSONDecodeError, sqlite3.Error) as exc:
            raise RuntimeError(f"Failed to fork Codex session: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()

    def owns_untracked_session(self, *, project_root: Path, session_ref: str) -> bool:
        return _owns_session(project_root, session_ref)

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        return extract_codex_report(artifacts, spawn_id)


register_harness_bundle(
    HarnessBundle(
        harness_id=HarnessId.CODEX,
        adapter=CodexAdapter(),
        spec_cls=ResolvedLaunchSpec,
        extractor=CODEX_EXTRACTOR,
        connections={TransportId.STREAMING: CodexConnection},
        projections=HarnessProjectionPorts(
            subprocess_cli_args=project_codex_spec_to_cli_args,
            managed_primary=ManagedPrimaryProjectionPorts(
                backend_command=project_codex_spec_to_appserver_command,
                bootstrap_payload=project_codex_spec_to_thread_request_for_project,
            ),
        ),
    )
)
