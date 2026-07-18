"""Spawn create-input validation and payload preparation helpers."""

import os
from dataclasses import dataclass

import structlog

from meridian.lib.config.settings import load_config
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.diagnostics import capture_library_diagnostics
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.composition_spawn import (
    bind_spawn_launch_context,
    compose_spawn_launch_surface,
)
from meridian.lib.launch.context import PreparedLaunchSurface, RuntimeBindings
from meridian.lib.launch.plan import build_spawn_mars_runtime
from meridian.lib.launch.reference import parse_template_assignments
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    RetryPolicy,
    SessionRequest,
    SpawnRequest,
    is_exact_continue_session,
)
from meridian.lib.launch.resolution import resolve_launch_inputs
from meridian.lib.launch.resolve import parse_duration_seconds
from meridian.lib.state.artifact_store import LocalStore
from meridian.lib.state.paths import resolve_project_paths
from meridian.lib.state.session_store import get_session_active_work_id
from meridian.lib.state.spawn.model import BACKGROUND_LAUNCH_MODE, FOREGROUND_LAUNCH_MODE

from ..runtime import (
    OperationRuntime,
    build_runtime,
    resolve_runtime_authority_for_read,
)
from .models import SpawnCreateInput
from .task_dir import derive_inheritable_task_dir

logger = structlog.get_logger(__name__)


def _parse_env_assignments(raw: tuple[str, ...]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in raw:
        if "=" not in item:
            raise ValueError(
                f"Invalid --env value {item!r}: expected KEY=VALUE format."
            )
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(
                f"Invalid --env value {item!r}: KEY is empty."
            )
        parsed[key] = value
    return parsed


def _resolve_spawn_env(payload: SpawnCreateInput) -> dict[str, str]:
    cli_env = _parse_env_assignments(payload.env)
    if cli_env:
        return cli_env
    if payload.launch_policy_snapshot is not None:
        return dict(payload.launch_policy_snapshot.env)
    return {}


def _is_exact_continue(payload: SpawnCreateInput) -> bool:
    return is_exact_continue_session(payload.session)


@dataclass(frozen=True)
class SpawnCreateArtifacts:
    """Resolved spawn request plus expensive prepared surface for bind-only execute."""

    request: SpawnRequest
    prepared: PreparedLaunchSurface


def validate_create_input(payload: SpawnCreateInput) -> tuple[SpawnCreateInput, str | None]:
    """Validate spawn input without leaking library warnings to stderr."""

    with capture_library_diagnostics():
        if not payload.prompt.strip() and not payload.files:
            raise ValueError("prompt required: pass -p/--prompt or --prompt-file")
        return payload, None


def build_create_payload(
    payload: SpawnCreateInput,
    *,
    runtime: OperationRuntime | None = None,
    preflight_warning: str | None = None,
    ctx: RuntimeContext | None = None,
) -> SpawnCreateArtifacts:
    """Build a spawn request and prepared launch surface for create/execute handoff."""

    with capture_library_diagnostics():
        if runtime is not None:
            if runtime.authority.runtime_root is None:
                raise ValueError("Operation runtime is missing runtime authority root.")
            project_root = runtime.project_root
            runtime_root = runtime.authority.runtime_root
            config = runtime.config
            harness_registry = runtime.harness_registry
        elif payload.dry_run:
            authority = resolve_runtime_authority_for_read(payload.project_root)
            project_root = authority.project_root
            runtime_root = authority.runtime_root
            config = load_config(project_root, authority=authority)
            harness_registry = get_default_harness_registry()
        else:
            runtime_bundle = build_runtime(payload.project_root)
            if runtime_bundle.authority.runtime_root is None:
                raise ValueError("Built operation runtime is missing runtime authority root.")
            project_root = runtime_bundle.project_root
            runtime_root = runtime_bundle.authority.runtime_root
            config = runtime_bundle.config
            harness_registry = runtime_bundle.harness_registry

        resolved_context = (
            ctx
            if ctx is not None
            else RuntimeContext.from_environment(
                project_root=project_root,
                runtime_root=runtime_root,
            )
        )
        explicit_work_id = payload.work.strip() or None
        inherit_ambient_work = not _is_exact_continue(payload)
        ambient_work_id = (
            (resolved_context.work_id or "").strip() or None
            if inherit_ambient_work
            else None
        )
        if (
            inherit_ambient_work
            and ambient_work_id is None
            and resolved_context.chat_id
            and runtime_root is not None
        ):
            try:
                ambient_work_id = (
                    get_session_active_work_id(runtime_root, resolved_context.chat_id) or ""
                ).strip() or None
            except Exception:
                ambient_work_id = None
        project_state_dir = resolve_project_paths(project_root).root_dir
        spawn_id = (
            str(resolved_context.spawn_id) if resolved_context.spawn_id is not None else None
        )
        inherited_for_child = (
            None
            if _is_exact_continue(payload)
            else derive_inheritable_task_dir(
                project_root=project_root,
                project_state_dir=project_state_dir,
                spawn_id=spawn_id,
                work_id=ambient_work_id,
            )
        )
        launch_resolution = resolve_launch_inputs(
            authority_root=project_root,
            project_state_dir=project_state_dir,
            context_from=payload.context_from,
            reference_files=tuple(str(path) for path in payload.files),
            explicit_task_dir=payload.task_dir,
            explicit_work_id=explicit_work_id,
            inherited_task_dir=inherited_for_child,
            ambient_work_id=ambient_work_id,
            caller_cwd=payload.caller_cwd,
        )
        parsed_template_vars = parse_template_assignments(payload.template_vars)
        resolved_work_id_hint = launch_resolution.effective_work_id

        raw_request = SpawnRequest(
            prompt=payload.prompt,
            prompt_is_composed=False,
            model=payload.model or None,
            harness=payload.harness,
            agent=payload.agent,
            agent_opt_out=payload.agent_opt_out,
            skills=payload.skills,
            extra_args=payload.passthrough_args,
            execution_policy=ResolvedExecutionPolicy(
                sandbox=payload.sandbox,
                approval=payload.approval,
                autocompact=payload.autocompact,
                autocompact_pct=payload.autocompact_pct,
                effort=payload.effort,
                timeout=payload.timeout,
                resident_rearm_budget=payload.resident_rearm_budget,
            ),
            retry=RetryPolicy(
                max_attempts=max(1, config.max_retries + 1),
                backoff_secs=config.retry_backoff_seconds,
            ),
            session=SessionRequest(
                continue_chat_id=payload.session.continue_chat_id,
                requested_harness_session_id=(
                    (payload.session.requested_harness_session_id or "").strip() or None
                ),
                continue_fork=payload.session.continue_fork,
                source_control_root=payload.session.source_control_root,
                source_execution_cwd=payload.session.source_execution_cwd,
                source_claude_config_dir=payload.session.source_claude_config_dir,
                source_pi_session_dir=payload.session.source_pi_session_dir,
                forked_from_chat_id=payload.session.forked_from_chat_id,
                continue_harness=payload.session.continue_harness,
                continue_source_tracked=payload.session.continue_source_tracked,
                continue_source_ref=payload.session.continue_source_ref,
            ),
            context_from=payload.context_from,
            reference_files=tuple(
                path.as_posix() for path in launch_resolution.reference_files
            ),
            template_vars=parsed_template_vars,
            goal=payload.goal,
            work_id_hint=resolved_work_id_hint,
            inherited_context_work_id=launch_resolution.context_work_id,
            warning=preflight_warning.strip() if preflight_warning is not None else None,
            authority_root=launch_resolution.directory_context.authority_root.as_posix(),
            task_cwd=launch_resolution.directory_context.logical_task_cwd.as_posix(),
            reference_anchor=launch_resolution.directory_context.reference_anchor.as_posix(),
            task_cwd_source=launch_resolution.directory_context.task_cwd_source,
            task_cwd_work_item=launch_resolution.directory_context.work_item,
            launch_policy_snapshot=payload.launch_policy_snapshot,
            pi_task_ping_interval_seconds=parse_duration_seconds(payload.task_ping_interval),
            pi_task_ping_reset_on_activity=payload.task_ping_reset_on_activity,
            env=_resolve_spawn_env(payload),
        )

        if runtime is not None:
            if runtime_root is None:
                raise ValueError("Operation runtime is missing runtime authority root.")
            preview_root = runtime_root
            mars_runtime_source = runtime
        else:
            resolved_authority = resolve_runtime_authority_for_read(project_root)
            preview_root = runtime_root or resolved_authority.user_home / "cache" / "dry-run"
            mars_runtime_source = OperationRuntime(
                project_root=project_root,
                authority=resolved_authority,
                config=config,
                harness_registry=harness_registry,
                artifacts=LocalStore(root_dir=preview_root / "artifacts"),
            )
        composition_dry_run = payload.dry_run
        preview_runtime = build_spawn_mars_runtime(
            runtime=mars_runtime_source,
            runtime_root=preview_root,
            control_root=project_root,
            execution_cwd=launch_resolution.directory_context.logical_task_cwd.as_posix(),
            argv_intent=(
                LaunchArgvIntent.REQUIRED
                if composition_dry_run
                else LaunchArgvIntent.SPEC_ONLY
            ),
        )
        logger.debug(
            "spawn_launcher_phase",
            phase="composition",
            launcher_pid=os.getpid(),
        )
        prepared_surface = compose_spawn_launch_surface(
            request=raw_request,
            runtime=preview_runtime,
            harness_registry=harness_registry,
            dry_run=composition_dry_run,
            launch_mode=(
                BACKGROUND_LAUNCH_MODE if payload.background else FOREGROUND_LAUNCH_MODE
            ),
        )
        logger.debug(
            "spawn_launcher_phase",
            phase="composition_complete",
            launcher_pid=os.getpid(),
        )
        if composition_dry_run:
            plan_overrides = dict(config.env)
            plan_overrides.update(raw_request.env)
            preview_context = bind_spawn_launch_context(
                prepared=prepared_surface,
                bindings=RuntimeBindings(
                    spawn_id="dry-run",
                    dry_run=True,
                    plan_overrides=plan_overrides,
                ),
                runtime=preview_runtime,
                harness_registry=harness_registry,
            )
            resolved_request = preview_context.resolved_request.model_copy(
                update={"cli_command": preview_context.binding.argv}
            )
        else:
            resolved_request = prepared_surface.request

        return SpawnCreateArtifacts(request=resolved_request, prepared=prepared_surface)


__all__ = [
    "SpawnCreateArtifacts",
    "build_create_payload",
    "validate_create_input",
]
