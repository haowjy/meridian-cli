"""OpenCode CLI harness adapter.

Upstream moved from ``opencode-ai/opencode`` to ``anomalyco/opencode`` (opencode.ai).
The Meridian adapter targets current opencode.ai CLI releases.
"""

import logging
import re
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import ClassVar, Literal, cast

from meridian.lib.core.domain import SpawnStatus, TokenUsage
from meridian.lib.core.native_identity import NativeIdentityPlan, NativeSessionUnavailable
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
from meridian.lib.harness.connections.base import (
    PrimaryRuntimeEventSurface,
    PrimaryRuntimeRequestPolicy,
    RawHarnessEvent,
    ServerRequestHandler,
)
from meridian.lib.harness.connections.opencode_connection import OpenCodeConnection
from meridian.lib.harness.extractors.opencode import OPENCODE_EXTRACTOR
from meridian.lib.harness.launch_types import ManagedPrimaryPreview, SessionSeed
from meridian.lib.harness.opencode_backend import resolve_opencode_version
from meridian.lib.harness.opencode_report import (
    extract_opencode_report,
    extract_opencode_session_id,
    extract_opencode_session_id_from_artifacts,
)
from meridian.lib.harness.opencode_storage import (
    resolve_opencode_home_dir,
    resolve_opencode_storage_root,
)
from meridian.lib.harness.opencode_transcript import (
    detect_opencode_db_schema,
    opencode_db_any_session_exists,
    resolve_opencode_db_path,
)
from meridian.lib.harness.passthrough.opencode import (
    build_opencode_attach_command,
    build_opencode_server_attach_command,
)
from meridian.lib.harness.permission_broker import PermissionBroker
from meridian.lib.harness.projections.project_opencode_streaming import (
    opencode_model_parts,
    project_opencode_spec_to_serve_command,
    project_opencode_spec_to_session_payload,
)
from meridian.lib.harness.projections.project_opencode_subprocess import (
    project_opencode_spec_to_cli_args,
)
from meridian.lib.harness.semantics import (
    MERIDIAN_CONNECTION_CLOSED_EVENT,
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
    BASE_COMMAND_OPENCODE_SUBPROCESS,
    PRIMARY_BASE_COMMAND_OPENCODE,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec, TerminalSurfaceMode
from meridian.lib.launch.request import SessionRequest
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
    if not normalized:
        return ""
    provider, model_name = opencode_model_parts(normalized)
    return f"{provider.strip()}/{model_name.strip()}"


def _opencode_db_path() -> Path:
    return resolve_opencode_home_dir() / "opencode.db"


def _opencode_session_diff_path(session_id: str) -> Path:
    return resolve_opencode_home_dir() / "storage" / "session_diff" / f"{session_id}.json"


def _directory_matches_project(directory: str, project_root: Path) -> bool:
    if not directory.strip():
        return False
    try:
        return Path(directory).expanduser().resolve() == project_root.resolve()
    except OSError:
        return False


def project_opencode_spec_to_session_payload_for_project(
    spec: ResolvedLaunchSpec,
    *,
    project_root: Path,
) -> dict[str, object]:
    """Project OpenCode managed-primary bootstrap payload for one project root."""

    _ = project_root
    return project_opencode_spec_to_session_payload(spec)


def project_opencode_primary_preview(
    spec: ResolvedLaunchSpec,
    *,
    project_root: Path,
    env: Mapping[str, str] | None = None,
) -> ManagedPrimaryPreview:
    version = resolve_opencode_version(
        (env or {}).get("MERIDIAN_HARNESS_OPENCODE_VERSION"),
        binary="opencode",
    )
    backend = project_opencode_spec_to_serve_command(spec, host="127.0.0.1", port=0)
    backend[backend.index("--port") + 1] = "<port>"
    continuing = bool(spec.continue_session_id)
    if version == "v2":
        probe_step = (
            "GET /api/config, /api/provider and /api/agent; "
            "apply the requested model via POST /api/session/{id}/model.",
        )
        attach_command = build_opencode_server_attach_command(
            spec.continue_session_id or "<session>", "http://127.0.0.1:<port>"
        )
        bootstrap_path = (
            f"/api/session/{spec.continue_session_id}" if continuing else "/api/session"
        )
    else:
        probe_step = (
            "GET /config/providers, /config and /agent; "
            "validate availability and effective defaults.",
            "If its model conflicts, stop the owned backend and restart once "
            "with a launch-local agent override, then repeat all three inspections.",
        )
        attach_command = build_opencode_attach_command(
            spec.continue_session_id or "<session>", "http://127.0.0.1:<port>"
        )
        bootstrap_path = f"/session/{spec.continue_session_id}" if continuing else "/session"
    steps = (
        ("Preserve the existing native session's committed agent/model.",)
        if continuing
        else (*probe_step,)
        if spec.model
        else ("Use native model defaults.",)
    )
    return ManagedPrimaryPreview(
        backend_command=tuple(backend),
        bootstrap_method="GET" if continuing else "POST",
        bootstrap_path=bootstrap_path,
        bootstrap_payload={}
        if continuing
        else project_opencode_spec_to_session_payload_for_project(spec, project_root=project_root),
        attach_command=attach_command,
        steps=(
            *steps,
            "Preserve inherited configuration; add private system instructions when supplied.",
            "Launch, inspections, replacement and bootstrap share one startup deadline.",
            "Native configuration and actual message model are unavailable in dry-run.",
            "Fail on rejected configuration or uncertain cleanup; no black-box fallback.",
        ),
        model=spec.model if not continuing else None,
    )


def _legacy_owns_session(project_root: Path, session_ref: str) -> bool:
    opencode_logs = resolve_opencode_home_dir() / "log"
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
            "model_override_explicit",
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
            "context_from_payload",
            "reference_items",
            "task_cwd",
            "pi_harness_profile",
            "claude_allow_builtin_agents",
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
                runtime_hitl=RuntimeHitlMode.CONNECTION_REQUESTS,
                subprocess_permission_flags_projected_by_shared_policy=False,
                default_runtime_request_policy="auto_accept",
                primary_session_runtime_request_policy=(PrimaryRuntimeRequestPolicy.SURFACE_EVENTS),
                primary_session_runtime_event_surface=(
                    PrimaryRuntimeEventSurface.CONNECTION_EVENT_STREAM
                ),
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
            # Both wired streaming transports reject ``continue_fork``
            # (``opencode_http.py`` / ``opencode_v2_http.py`` ``_create_session``),
            # so advertising fork here would let launch policy carry it forward
            # only to fail at the connection. The policy layer downgrades an
            # unsupported fork to in-place resume with a warning.
            supports_session_fork=False,
            supports_native_skills=True,
            supports_primary_launch=True,
            # V2 applies an explicit model on resume via ``POST /api/session/{id}/model``.
            # V1 cannot switch the model on resume: the streaming transport fails
            # loudly in ``OpenCodeV1Connection._create_session``, and the
            # subprocess projector fails loudly for the same request rather
            # than forwarding ``--model``.
            supports_named_primary_resume=True,
            supports_native_file_injection=False,
            terminal_surface_modes=(
                TerminalSurfaceMode.PTY_MEDIATED,
                TerminalSurfaceMode.NATIVE_INHERIT,
            ),
            default_terminal_surface_mode=TerminalSurfaceMode.PTY_MEDIATED,
        )

    def run_prompt_policy(self) -> RunPromptPolicy:
        return RunPromptPolicy()

    def plan_native_identity(self, run: SpawnParams) -> NativeIdentityPlan | None:
        source = (run.continue_harness_session_id or "").strip()
        if source:
            return NativeIdentityPlan(source, None, None, "resume")
        return NativeIdentityPlan(None, None, None, "create")

    def finalize_native_identity(
        self, plan: NativeIdentityPlan, *, child_env: dict[str, str], child_cwd: Path,
        session: SessionRequest, spawn_id: SpawnId, interactive: bool,
    ) -> NativeIdentityPlan:
        store = session.source_native_store
        if plan.operation != "create" and store:
            child_env["OPENCODE_DB"] = store
        elif plan.operation != "create" and session.continue_source_tracked:
            raise ValueError("native_transcript_missing: recorded source store is absent")
        store = self.native_store_for_launch(child_env=child_env, child_cwd=child_cwd)
        locator = None
        if plan.operation != "create" and session.source_native_store:
            source_id = session.requested_harness_session_id or plan.harness_session_id or ""
            source = self.resolve_native_session_file(
                project_root=child_cwd, session_id=source_id, native_store=Path(store),
            )
            if source is None:
                raise ValueError(f"native_transcript_missing: {source_id}")
            locator = str(source)
        return replace(plan, native_store=store, locator=locator)

    def native_store_for_launch(self, *, child_env: dict[str, str], child_cwd: Path) -> str:
        database = resolve_opencode_db_path(child_env)
        if str(database) == ":memory:":
            raise ValueError(
                "native_transcript_missing: in-memory OpenCode stores cannot be tracked"
            )
        if not database.is_absolute():
            database = child_cwd / database
        child_env["OPENCODE_DB"] = str(database.resolve())
        return str(database.resolve())

    def resolve_launch_spec(
        self,
        run: SpawnParams,
        perms: PermissionResolver,
    ) -> ResolvedLaunchSpec:
        continue_session_id = (run.continue_harness_session_id or "").strip() or None
        identity_plan = self.plan_native_identity(run)
        normalized_model: str | None = None
        if run.model:
            normalized_model = _normalize_opencode_model(str(run.model)) or None
        if (
            normalized_model is not None
            and continue_session_id is not None
            and not run.continue_fork
            and not run.model_override_explicit
        ):
            # Exact continue replays the session's own model, and OpenCode resume
            # keeps the committed model, so a replayed token is not a switch
            # request. Drop it: V1 cannot change the model on resume and would
            # otherwise fail loudly on a no-op. An explicit ``--model`` override
            # is kept and still reaches the V1 guards (the streaming
            # ``_create_session`` raise and the ``opencode run`` subprocess
            # projector); V2 applies it via ``POST /api/session/{id}/model``.
            normalized_model = None
        return ResolvedLaunchSpec(
            harness=HarnessId.OPENCODE,
            model=normalized_model,
            effort=run.effort,
            prompt=run.user_turn_content or run.prompt,
            continue_session_id=continue_session_id,
            native_identity_plan=identity_plan,
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

    def build_primary_runtime_request_handler(
        self,
        *,
        spawn_dir: Path,
        event_sink: Callable[[RawHarnessEvent], Awaitable[None]],
    ) -> ServerRequestHandler | None:
        return PermissionBroker(
            spawn_dir=spawn_dir,
            event_sink=event_sink,
            auto_reject_runtime_requests=False,
            harness_id=HarnessId.OPENCODE.value,
        )

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

    def native_transcript_kind(self, path: Path) -> Literal["native_file", "opencode_db"]:
        return "opencode_db" if detect_opencode_db_schema(path) is not None else "native_file"

    def resolve_native_session_file(
        self, *, project_root: Path, session_id: str, native_store: Path,
    ) -> Path | None:
        return native_store if opencode_db_any_session_exists(
            session_id=session_id, db_path=native_store,
        ) else None

    def resolve_session_file(
        self,
        *,
        project_root: Path,
        session_id: str,
        config_root_hint: Path | None = None,
    ) -> Path | None:
        _ = project_root
        normalized = session_id.strip()
        if not normalized:
            return None
        storage_root = config_root_hint or resolve_opencode_storage_root()
        database = (storage_root.parent / "opencode.db"
                    if config_root_hint is not None else resolve_opencode_db_path())
        if opencode_db_any_session_exists(session_id=normalized, db_path=database):
            return database
        matches = [
            storage_root / folder / f"{normalized}.json"
            for folder in ("session_diff", "session")
            if (storage_root / folder / f"{normalized}.json").is_file()
        ]
        if len(matches) > 1:
            raise NativeSessionUnavailable(normalized, "ambiguous_native_file")
        if not matches:
            return None
        return matches[0]

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        _ = self
        return extract_opencode_session_id_from_artifacts(artifacts, spawn_id)

    def owns_untracked_session(self, *, project_root: Path, session_ref: str) -> bool:
        return _owns_session(project_root, session_ref)

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        return extract_opencode_report(artifacts, spawn_id)


def _resolve_opencode_terminal(event: RawHarnessEvent) -> TerminalEventOutcome | None:
    if event.event_type == MERIDIAN_CONNECTION_CLOSED_EVENT:
        return connection_closed_outcome(event)
    if event.event_type == "session.execution.failed":
        detail = event.payload.get("error") or event.payload.get("message")
        return TerminalEventOutcome(
            status=SpawnStatus.FAILED,
            exit_code=1,
            error=str(detail) if detail else "opencode_session_failed",
        )
    if event.event_type != "session.error":
        return None
    properties = event.payload.get("properties")
    error = (
        stringify_terminal_error(cast("dict[str, object]", properties))
        if isinstance(properties, dict)
        else stringify_terminal_error(event.payload.get("error"))
    )
    return TerminalEventOutcome(
        status=SpawnStatus.FAILED,
        exit_code=1,
        error=error or "opencode_session_error",
    )


_OPENCODE_V2_ACTIVITY_EVENTS: tuple[str, ...] = (
    "session.text.started",
    "session.text.delta",
    "session.text.ended",
    "session.reasoning.started",
    "session.reasoning.delta",
    "session.reasoning.ended",
    "session.step.started",
    "session.step.ended",
)

# OpenCode 2 emits ``session.execution.*`` instead of ``session.idle``; the
# version dispatcher only routes 2.x streams here, so both tables can coexist.
_OPENCODE_V2_EVENT_SEMANTICS: dict[str, EventSemantics] = {
    **{name: EventSemantics(activity="turn_active") for name in _OPENCODE_V2_ACTIVITY_EVENTS},
    "session.execution.succeeded": EventSemantics(
        activity="idle",
        clears_signal=True,
        terminal=TerminalEventOutcome(status=SpawnStatus.SUCCEEDED, exit_code=0),
    ),
    "session.execution.failed": EventSemantics(activity="idle", clears_signal=True),
    # A user-initiated cancel already transitions the connection to ``stopping``
    # (via ``send_cancel``) before the server emits ``interrupted``, so the normal
    # Meridian stop path ends the drain on state — not on this event. An *unpaired*
    # interrupt (guardrail stop, internal abort) arrives with the connection still
    # ``connected``; give it a terminal so the spawn fails fast instead of idling
    # ~120s into a liveness ``connectionClosed``.
    "session.execution.interrupted": EventSemantics(
        activity="idle",
        clears_signal=True,
        terminal=TerminalEventOutcome(
            status=SpawnStatus.FAILED,
            exit_code=1,
            error="opencode_session_interrupted",
        ),
    ),
}

OPENCODE_SEMANTICS = HarnessSemantics(
    events={
        "agent_message_chunk": EventSemantics(activity="turn_active"),
        "agent_thought_chunk": EventSemantics(activity="turn_active"),
        "tool_call": EventSemantics(activity="turn_active"),
        "tool_call_update": EventSemantics(activity="turn_active"),
        "session.idle": EventSemantics(
            activity="idle",
            clears_signal=True,
            terminal=TerminalEventOutcome(status=SpawnStatus.SUCCEEDED, exit_code=0),
        ),
        "session.error": EventSemantics(clears_signal=True),
        **_OPENCODE_V2_EVENT_SEMANTICS,
        MERIDIAN_CONNECTION_CLOSED_EVENT: EventSemantics(),
    },
    payload_resolvers={
        "session.error": _resolve_opencode_terminal,
        "session.execution.failed": _resolve_opencode_terminal,
        MERIDIAN_CONNECTION_CLOSED_EVENT: _resolve_opencode_terminal,
    },
    scoped_events=frozenset(
        {
            "agent_message_chunk",
            "agent_thought_chunk",
            "tool_call",
            "tool_call_update",
            "session.idle",
            "session.error",
            *_OPENCODE_V2_ACTIVITY_EVENTS,
            "session.execution.succeeded",
            "session.execution.failed",
            "session.execution.interrupted",
        }
    ),
    scope_id_resolver=extract_opencode_session_id,
)

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
                preview=project_opencode_primary_preview,
            ),
        ),
        semantics=OPENCODE_SEMANTICS,
    )
)
