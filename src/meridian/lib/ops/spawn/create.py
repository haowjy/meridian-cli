"""`spawn create`: resolve, prepare and launch a new spawn."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meridian.lib.bootstrap.services import (
    prepare_for_runtime_write,
)
from meridian.lib.config.loader import load_config
from meridian.lib.core.caller_boundary import CallerBoundaryCheck
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.context_ref import context_ref_values
from meridian.lib.core.depth import max_depth_reached
from meridian.lib.core.sink import NullSink, OutputSink
from meridian.lib.harness.bundle import project_effective_policy
from meridian.lib.launch.continue_launch import (
    ContinueHold,
)
from meridian.lib.ops.runtime import (
    OperationRuntime,
    inspect_runtime_authority_for_read,
    plan_runtime_authority_for_write,
    runtime_context,
)
from meridian.lib.state import work_store
from meridian.lib.state.paths import resolve_project_paths
from meridian.lib.state.work_state import slugify
from meridian.lib.telemetry.init import setup_telemetry
from meridian.lib.telemetry.observer import register_spawn_telemetry_observer
from meridian.lib.telemetry.observers import register_debug_trace_observer
from meridian.lib.telemetry.router import emit_telemetry

from .execute import (
    depth_exceeded_output,
    depth_limits,
    execute_spawn_background,
    execute_spawn_blocking,
)
from .models import (
    SpawnActionOutput,
    SpawnContinueInput,
    SpawnCreateInput,
    SpawnForkInput,
)
from .pre_init import (
    EXPECTED_PRE_INIT_EXCEPTIONS,
    PreInitFailure,
    run_pre_init_boundary,
)
from .prepare import (
    SpawnCreateArtifacts,
    bind_create_payload_context,
    bind_create_payload_preview,
    build_create_payload,
    validate_create_input,
)


def _emit_usage_spawn_launched(*, harness: str | None, spawn_id: str | None = None) -> None:
    normalized_harness = (harness or "").strip()
    if not normalized_harness:
        return
    emit_telemetry(
        "usage",
        "usage.spawn.launched",
        scope="core.launch",
        ids={"spawn_id": spawn_id} if spawn_id is not None else None,
        data={"harness": normalized_harness},
    )


def _forked_from_output(payload: SpawnCreateInput) -> str | None:
    if not payload.session.continue_fork:
        return None

    source_chat_id = (payload.session.forked_from_chat_id or "").strip()
    if source_chat_id:
        return source_chat_id
    source_ref = (payload.session.continue_source_ref or "").strip()
    if source_ref:
        return source_ref
    return None


def _validate_exact_work_id(work_id: str) -> str:
    normalized = slugify(work_id)
    if not normalized or normalized != work_id:
        raise ValueError(
            f"Invalid work item name '{work_id}'. "
            f"Use a slug (lowercase, hyphens, no spaces) — e.g. '{normalized or 'my-feature'}'."
        )
    return normalized


def _lookup_explicit_work_item(
    *,
    project_state_dir: Path,
    work_id: str,
) -> tuple[str, bool]:
    resolved_work_id = _validate_exact_work_id(work_id)
    return (
        resolved_work_id,
        work_store.get_work_item(project_state_dir, resolved_work_id) is not None,
    )


def merge_warnings(*warnings: str | None) -> str | None:
    merged = [warning.strip() for warning in warnings if warning and warning.strip()]
    if not merged:
        return None
    return " ".join(merged)


@dataclass(frozen=True)
class SpawnCreatePreparation:
    payload: SpawnCreateInput
    resolved_context: RuntimeContext
    authority: Any
    planning_runtime: OperationRuntime | None
    artifacts: SpawnCreateArtifacts | None
    dry_run_work_warning: str | None
    depth_exceeded: SpawnActionOutput | None = None


def _failure_create_input(
    payload: SpawnCreateInput | SpawnContinueInput | SpawnForkInput,
) -> SpawnCreateInput:

    if isinstance(payload, SpawnCreateInput):
        return payload
    return SpawnCreateInput(
        prompt=payload.prompt,
        model=payload.model,
        files=payload.files,
        template_vars=payload.template_vars,
        agent=payload.agent,
        agent_opt_out=payload.agent_opt_out,
        skills=payload.skills,
        goal=payload.goal,
        desc=payload.desc,
        work=payload.work,
        task_dir=payload.task_dir,
        caller_cwd=payload.caller_cwd,
        **payload.launch_option_updates(),
    )


def run_spawn_creation_boundary(
    payload: SpawnCreateInput | SpawnContinueInput | SpawnForkInput,
    *,
    existing_check: CallerBoundaryCheck | None = None,
    replayed_passthrough_args: tuple[str, ...] = (),
) -> CallerBoundaryCheck | SpawnActionOutput:
    """Run a caller-boundary phase and present every refusal as pre-init failure."""

    failure_payload = _failure_create_input(payload)

    def _build() -> CallerBoundaryCheck:
        try:
            if existing_check is not None:
                existing_check.enforce_passthrough_args(replayed_passthrough_args)
                return existing_check
            return CallerBoundaryCheck.from_environment(
                dry_run=payload.dry_run,
                passthrough_args=payload.passthrough_args,
            )
        except EXPECTED_PRE_INIT_EXCEPTIONS as exc:
            raise PreInitFailure(str(exc)) from exc

    return run_pre_init_boundary(payload=failure_payload, operation=_build)


def _prepare_spawn_create(
    *,
    payload: SpawnCreateInput,
    ctx: RuntimeContext | None,
    sink: OutputSink | None,
    floor_check: CallerBoundaryCheck,
    failure_payload: list[SpawnCreateInput],
) -> SpawnCreatePreparation:
    register_debug_trace_observer()
    resolved_context = runtime_context(ctx)
    logical_owner = str(resolved_context.spawn_id or "") or "cli"
    authority = inspect_runtime_authority_for_read(payload.project_root)
    planning_authority = plan_runtime_authority_for_write(authority.project_root)
    resolved_root = authority.project_root
    config = load_config(resolved_root, authority=authority)
    if payload.dry_run:
        setup_telemetry(runtime_root=None, logical_owner=logical_owner)
        register_spawn_telemetry_observer()
    payload = payload.model_copy(update={"project_root": resolved_root.as_posix()})
    failure_payload[0] = payload
    payload, preflight_warning = validate_create_input(payload)
    failure_payload[0] = payload
    dry_run_work_warning: str | None = None
    if payload.dry_run and payload.work.strip():
        project_local_root = resolve_project_paths(resolved_root).root_dir
        resolved_work_id, work_exists = _lookup_explicit_work_item(
            project_state_dir=project_local_root,
            work_id=payload.work,
        )
        payload = payload.model_copy(update={"work": resolved_work_id})
        failure_payload[0] = payload
        if not work_exists:
            dry_run_work_warning = (
                f"Work item '{resolved_work_id}' does not exist. "
                "Dry-run leaves state unchanged; it would be created on launch."
            )

    if not payload.dry_run:
        current_depth, max_depth = depth_limits(config.max_depth, ctx=resolved_context)
        if max_depth_reached(current_depth, max_depth):
            return SpawnCreatePreparation(
                payload=payload,
                resolved_context=resolved_context,
                authority=authority,
                planning_runtime=None,
                artifacts=None,
                dry_run_work_warning=dry_run_work_warning,
                depth_exceeded=depth_exceeded_output(current_depth, max_depth),
            )

    from meridian.lib.harness.registry import get_default_harness_registry

    harness_registry = get_default_harness_registry()
    planned_runtime_root = planning_authority.runtime_root
    if planned_runtime_root is None:
        from meridian.lib.state.user_paths import get_dry_run_preview_root

        planned_runtime_root = get_dry_run_preview_root(user_home=planning_authority.user_home)
        planning_authority = planning_authority.model_copy(
            update={"runtime_root": planned_runtime_root}
        )
    planning_runtime = OperationRuntime(
        project_root=resolved_root,
        authority=planning_authority,
        config=config,
        harness_registry=harness_registry,
        sink=sink or NullSink(),
    )

    artifacts = build_create_payload(
        payload,
        runtime=planning_runtime,
        preflight_warning=preflight_warning,
        ctx=resolved_context,
        models_readonly=True,
    )
    preview_context = None
    if floor_check.caller is not None:
        preview_context = bind_create_payload_context(
            artifacts,
            harness_registry=harness_registry,
        )
        spec = preview_context.binding.spec
        child_harness = artifacts.prepared.harness.id
        floor_check.enforce(
            project_effective_policy(
                child_harness,
                spec.permission_policy,
                native_config=spec.native_config,
            ),
            requested_sandbox=spec.permission_policy.sandbox,
            harness=str(child_harness),
        )
    if payload.dry_run:
        artifacts = bind_create_payload_preview(
            artifacts,
            harness_registry=harness_registry,
            preview_context=preview_context,
        )
    return SpawnCreatePreparation(
        payload=payload,
        resolved_context=resolved_context,
        authority=authority,
        planning_runtime=planning_runtime,
        artifacts=artifacts,
        dry_run_work_warning=dry_run_work_warning,
    )


def _prepare_spawn_create_with_expected_failures(
    *,
    payload: SpawnCreateInput,
    ctx: RuntimeContext | None,
    sink: OutputSink | None,
    floor_check: CallerBoundaryCheck,
    failure_payload: list[SpawnCreateInput],
) -> SpawnCreatePreparation:
    try:
        return _prepare_spawn_create(
            payload=payload,
            ctx=ctx,
            sink=sink,
            floor_check=floor_check,
            failure_payload=failure_payload,
        )
    except (*EXPECTED_PRE_INIT_EXCEPTIONS, RuntimeError) as exc:
        raise PreInitFailure(str(exc)) from exc


def run_spawn_create_preparation(
    payload: SpawnCreateInput,
    *,
    ctx: RuntimeContext | None,
    floor_check: CallerBoundaryCheck,
    sink: OutputSink | None = None,
) -> SpawnCreatePreparation | SpawnActionOutput:
    failure_payload = [payload]
    return run_pre_init_boundary(
        payload=lambda: failure_payload[0],
        operation=lambda: _prepare_spawn_create_with_expected_failures(
            payload=payload,
            ctx=ctx,
            sink=sink,
            floor_check=floor_check,
            failure_payload=failure_payload,
        ),
    )


def _materialize_spawn_runtime(
    preparation: SpawnCreatePreparation,
    *,
    sink: OutputSink | None,
) -> tuple[OperationRuntime, Any]:
    planning_runtime = preparation.planning_runtime
    if planning_runtime is None:
        raise RuntimeError("Spawn create preparation did not produce a runtime plan.")
    prepared_context = prepare_for_runtime_write(preparation.authority.project_root)
    runtime_root = prepared_context.runtime_root
    if runtime_root is None:
        raise RuntimeError("Prepared runtime context is missing runtime root.")
    setup_telemetry(
        runtime_root=runtime_root,
        logical_owner=str(preparation.resolved_context.spawn_id or "") or "cli",
    )
    register_spawn_telemetry_observer()
    runtime = OperationRuntime(
        project_root=prepared_context.project_root,
        authority=prepared_context.authority,
        config=planning_runtime.config,
        harness_registry=planning_runtime.harness_registry,
        sink=sink or NullSink(),
    )
    return runtime, prepared_context.authority


def execute_prepared_spawn_create(
    preparation: SpawnCreatePreparation,
    *,
    sink: OutputSink | None,
    on_spawn_id: Callable[[str], None] | None = None,
    hold: ContinueHold | None = None,
) -> SpawnActionOutput:
    if preparation.depth_exceeded is not None:
        return preparation.depth_exceeded

    payload = preparation.payload
    resolved_context = preparation.resolved_context
    authority = preparation.authority
    artifacts = preparation.artifacts
    dry_run_work_warning = preparation.dry_run_work_warning
    if artifacts is None:
        raise RuntimeError("Spawn create preparation did not produce artifacts.")
    prepared_request = artifacts.request
    prepared_surface = artifacts.prepared
    forked_from = _forked_from_output(payload)
    if payload.dry_run:
        prepared_goal = getattr(prepared_request, "goal", payload.goal)
        agent_metadata = getattr(prepared_request, "agent_metadata", {}) or {}
        terminal_surface_mode = getattr(prepared_request, "terminal_surface_mode", None)
        _emit_usage_spawn_launched(harness=prepared_request.harness)
        return SpawnActionOutput(
            command="spawn.create",
            status="dry-run",
            model=prepared_request.model or "",
            harness_id=prepared_request.harness or "",
            warning=merge_warnings(prepared_request.warning, dry_run_work_warning),
            agent=prepared_request.agent,
            agent_path=agent_metadata.get("session_agent_path") or None,
            skills=prepared_request.skills,
            skill_paths=prepared_request.skill_paths,
            reference_files=prepared_request.reference_files,
            template_vars=prepared_request.template_vars,
            context_from_resolved=context_ref_values(prepared_request.context_refs),
            composed_prompt=prepared_surface.content.final_prompt,
            goal=prepared_goal,
            model_selection_requested_token=prepared_request.model_selection_requested_token,
            model_selection_canonical_id=prepared_request.model_selection_canonical_id,
            model_selection_harness_provenance=(
                prepared_request.model_selection_harness_provenance
            ),
            matched_policy_rule=getattr(prepared_request, "matched_policy_rule", None),
            selection_report=prepared_request.selection_report,
            terminal_surface_mode=(
                terminal_surface_mode.value if terminal_surface_mode is not None else None
            ),
            project_root=authority.project_root.as_posix(),
            project_root_source=authority.project_root_source,
            runtime_root=authority.runtime_root.as_posix()
            if authority.runtime_root is not None
            else None,
            runtime_root_source=authority.runtime_root_source,
            authority_root=getattr(prepared_request, "authority_root", None),
            task_cwd=getattr(prepared_request, "task_cwd", None),
            reference_anchor=getattr(prepared_request, "reference_anchor", None),
            task_cwd_source=getattr(prepared_request, "task_cwd_source", None),
            task_cwd_work_item=getattr(prepared_request, "task_cwd_work_item", None),
            cli_command=tuple(getattr(prepared_request, "cli_command", ()) or ()),
            permission_report=prepared_request.permission_report,
            message="Dry run complete.",
            forked_from=forked_from,
        )

    runtime, authority = _materialize_spawn_runtime(preparation, sink=sink)
    if payload.background:
        result = execute_spawn_background(
            payload=payload,
            request=prepared_request,
            runtime=runtime,
            ctx=resolved_context,
            hold=hold,
        )
    else:
        result = execute_spawn_blocking(
            payload=payload,
            request=prepared_request,
            runtime=runtime,
            ctx=resolved_context,
            prepared=prepared_surface,
            on_spawn_id=on_spawn_id,
            hold=hold,
        )
    _emit_usage_spawn_launched(
        harness=result.harness_id or prepared_request.harness,
        spawn_id=result.spawn_id,
    )
    updates: dict[str, object] = {
        "identity_created": bool(authority.identity_created),
    }
    if forked_from is not None:
        updates["forked_from"] = forked_from
    return result.model_copy(update=updates)


def spawn_create_sync(
    payload: SpawnCreateInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    on_spawn_id: Callable[[str], None] | None = None,
) -> SpawnActionOutput:
    floor_check = run_spawn_creation_boundary(payload)
    if isinstance(floor_check, SpawnActionOutput):
        return floor_check
    preparation = run_spawn_create_preparation(
        payload,
        ctx=ctx,
        floor_check=floor_check,
        sink=sink,
    )
    if isinstance(preparation, SpawnActionOutput):
        return preparation
    return execute_prepared_spawn_create(
        preparation,
        sink=sink,
        on_spawn_id=on_spawn_id,
    )
