"""Spawn create-input validation and payload preparation helpers."""

from pathlib import Path

from meridian.lib.config.settings import load_config
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.diagnostics import capture_library_diagnostics
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.cwd import LaunchDirectoryContext, TaskCwdResolution, resolve_task_cwd
from meridian.lib.launch.reference import parse_template_assignments, validate_reference_paths
from meridian.lib.launch.request import (
    ExecutionBudget,
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    RetryPolicy,
    SessionRequest,
    SpawnRequest,
)
from meridian.lib.state.paths import resolve_kb_dir, resolve_project_paths
from meridian.lib.state.session_store import get_session_active_work_id
from meridian.lib.utils.time import minutes_to_seconds

from ..runtime import (
    OperationRuntime,
    build_runtime,
    resolve_runtime_authority_for_read,
    runtime_context,
)
from .models import SpawnCreateInput

_DRY_RUN_REPORT_PATH = "<spawn-report-path>"


def validate_create_input(payload: SpawnCreateInput) -> tuple[SpawnCreateInput, str | None]:
    """Validate spawn input without leaking library warnings to stderr."""

    with capture_library_diagnostics():
        if not payload.prompt.strip() and not payload.files:
            raise ValueError("prompt required: use --prompt/-p or attach at least one --file/-f.")
        return payload, None


def build_create_payload(
    payload: SpawnCreateInput,
    *,
    runtime: OperationRuntime | None = None,
    preflight_warning: str | None = None,
    ctx: RuntimeContext | None = None,
) -> SpawnRequest:
    """Build a spawn request without leaking library warnings to stderr."""

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
            config = load_config(project_root, authority=authority)
            runtime_root = authority.runtime_root or authority.project_state_dir
            harness_registry = get_default_harness_registry()
        else:
            runtime_bundle = build_runtime(payload.project_root)
            if runtime_bundle.authority.runtime_root is None:
                raise ValueError("Built operation runtime is missing runtime authority root.")
            project_root = runtime_bundle.project_root
            runtime_root = runtime_bundle.authority.runtime_root
            config = runtime_bundle.config
            harness_registry = runtime_bundle.harness_registry

        resolved_context = runtime_context(ctx)
        explicit_work_id = payload.work.strip() or None
        ambient_work_id = (resolved_context.work_id or "").strip() or None
        if ambient_work_id is None and resolved_context.chat_id:
            try:
                ambient_work_id = (
                    get_session_active_work_id(runtime_root, resolved_context.chat_id) or ""
                ).strip() or None
            except Exception:
                ambient_work_id = None
        project_state_dir = resolve_project_paths(project_root).root_dir
        selected_work_id = explicit_work_id or ambient_work_id
        if payload.predicted_task_cwd is not None:
            task_cwd_resolution = TaskCwdResolution(
                task_cwd=Path(payload.predicted_task_cwd),
                source="forced-worktree",
                work_item=selected_work_id,
            )
        else:
            task_cwd_resolution = resolve_task_cwd(
                project_root,
                project_state_dir=project_state_dir,
                explicit_work_id=explicit_work_id,
                ambient_work_id=ambient_work_id,
                force_worktree=payload.worktree is True,
                force_no_worktree=payload.worktree is False,
            )
        directory_context = LaunchDirectoryContext.from_task_cwd_resolution(
            authority_root=project_root,
            task_cwd_resolution=task_cwd_resolution,
        )
        validated_paths = validate_reference_paths(
            payload.files,
            reference_anchor=directory_context.reference_anchor,
            kb_dir=resolve_kb_dir(project_root),
        )
        parsed_template_vars = parse_template_assignments(payload.template_vars)
        timeout_secs = minutes_to_seconds(payload.timeout)
        kill_grace_secs = minutes_to_seconds(config.kill_grace_minutes) or 0.0

        raw_request = SpawnRequest(
            prompt=payload.prompt,
            prompt_is_composed=False,
            model=payload.model or None,
            harness=payload.harness,
            agent=payload.agent,
            skills=payload.skills,
            extra_args=payload.passthrough_args,
            execution_policy=ResolvedExecutionPolicy(
                sandbox=payload.sandbox,
                approval=payload.approval,
                autocompact=payload.autocompact,
                autocompact_pct=payload.autocompact_pct,
                effort=payload.effort,
            ),
            retry=RetryPolicy(
                max_attempts=max(1, config.max_retries + 1),
                backoff_secs=config.retry_backoff_seconds,
            ),
            budget=ExecutionBudget(
                timeout_secs=int(timeout_secs) if timeout_secs is not None else None,
                kill_grace_secs=int(kill_grace_secs),
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
            reference_files=tuple(str(p) for p in validated_paths),
            template_vars=parsed_template_vars,
            goal=payload.goal,
            work_id_hint=payload.work.strip() or None,
            warning=preflight_warning.strip() if preflight_warning is not None else None,
            authority_root=directory_context.authority_root.as_posix(),
            task_cwd=directory_context.logical_task_cwd.as_posix(),
            reference_anchor=directory_context.reference_anchor.as_posix(),
            task_cwd_source=directory_context.task_cwd_source,
            task_cwd_work_item=directory_context.work_item,
        )

        preview_context = build_launch_context(
            spawn_id="dry-run",
            request=raw_request,
            runtime=LaunchRuntime(
                argv_intent=LaunchArgvIntent.REQUIRED,
                composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
                config_snapshot=config.model_dump(mode="json", exclude_none=True),
                runtime_override_snapshot=RuntimeOverrides.from_env().model_dump(
                    mode="json",
                    exclude_none=True,
            ),
            report_output_path=_DRY_RUN_REPORT_PATH,
            runtime_root=runtime_root.as_posix(),
            config_root=project_root.as_posix(),
            control_root=project_root.as_posix(),
            requested_task_cwd=directory_context.logical_task_cwd.as_posix(),
            # Legacy aliases.
            project_paths_project_root=project_root.as_posix(),
            project_paths_execution_cwd=directory_context.logical_task_cwd.as_posix(),
        ),
            harness_registry=harness_registry,
            dry_run=True,
        )
        return preview_context.resolved_request.model_copy(
            update={"cli_command": preview_context.binding.argv}
        )


__all__ = ["build_create_payload", "validate_create_input"]
