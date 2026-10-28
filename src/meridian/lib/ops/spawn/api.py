"""Spawn operations used by CLI and MCP surfaces."""

import asyncio
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meridian.lib.bootstrap.services import (
    RuntimeReadContext,
    RuntimeWriteContext,
    build_spawn_application_service,
    build_spawn_application_service_from_roots,
    prepare_for_runtime_write,
)
from meridian.lib.config.loader import load_config
from meridian.lib.config.models import MeridianConfig
from meridian.lib.core.caller_boundary import CallerBoundaryCheck
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.context_ref import context_ref_values
from meridian.lib.core.depth import max_depth_reached
from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.native_identity import NativeSessionUnavailable
from meridian.lib.core.sink import NullSink, OutputSink
from meridian.lib.core.spawn_lifecycle import (
    ACTIVE_SPAWN_STATUSES,
    ALL_SPAWN_STATUSES,
    FAILURE_SPAWN_STATUSES,
    TERMINAL_SPAWN_STATUSES,
    is_active_spawn_status,
    is_terminal_spawn_status,
)
from meridian.lib.core.spawn_service import CancelOutcome
from meridian.lib.core.spawn_start import resolve_spawn_display_label
from meridian.lib.core.telemetry import register_debug_trace_observer
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.bundle import project_effective_policy
from meridian.lib.launch.continue_replay import (
    build_continue_replay_contract,
    continue_replay_source_from_reference,
)
from meridian.lib.launch.continue_sweep import sweep_before_continue
from meridian.lib.launch.request import SessionRequest, cross_harness_continue_error
from meridian.lib.ops.locate import resolve_spawn_target, resolve_spawn_targets
from meridian.lib.ops.mars import mars_agent_subagents, mars_list_subagents
from meridian.lib.ops.reference import ResolvedSessionReference, resolve_session_reference
from meridian.lib.ops.runtime import (
    OperationRuntime,
    inspect_runtime_authority_for_read,
    plan_runtime_authority_for_write,
    resolve_project_authority,
    resolve_runtime_root,
    resolve_runtime_root_and_config,
    resolve_runtime_root_and_config_for_read,
    resolve_runtime_root_for_read,
    runtime_context,
)
from meridian.lib.state import history_index, session_store, spawn_store, work_store
from meridian.lib.state.history_index import indexed_spawn_scan
from meridian.lib.state.paths import resolve_project_paths
from meridian.lib.state.primary_meta import (
    read_primary_surface_metadata,
)
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_observations import record as record_spawn_observation
from meridian.lib.state.spawn_ownership import owned_parent_ids
from meridian.lib.state.spawn_signals import (
    SpawnSignalKind,
    read_accepted_signals,
    write_spawn_signal,
)
from meridian.lib.state.spawn_status import overlay_derived, read_derived
from meridian.lib.state.spawn_tree import collect_descendants, descendant_id_set
from meridian.lib.state.work_state import slugify
from meridian.lib.telemetry.init import setup_telemetry
from meridian.lib.telemetry.observer import register_spawn_telemetry_observer
from meridian.lib.telemetry.router import emit_telemetry

from .execute import (
    depth_exceeded_output,
    depth_limits,
    execute_spawn_background,
    execute_spawn_blocking,
)
from .models import (
    ModelStats,
    SpawnActionOutput,
    SpawnCancelAllInput,
    SpawnCancelAllOutput,
    SpawnCancelInput,
    SpawnChildrenInput,
    SpawnContinueInput,
    SpawnCreateInput,
    SpawnDetailOutput,
    SpawnForkInput,
    SpawnListEntry,
    SpawnListInput,
    SpawnListOutput,
    SpawnShowInput,
    SpawnSignalInput,
    SpawnStatsChild,
    SpawnStatsInput,
    SpawnStatsOutput,
    SpawnStatusInput,
    SpawnSubagentEntry,
    SpawnSubagentsInput,
    SpawnSubagentsOutput,
    SpawnWaitInput,
    SpawnWaitMultiOutput,
    format_spawn_identity_table,
)
from .owed import (
    consume_result_if_shown,
    record_result_shown_to_caller,
    resolve_owed_finished_rows,
    select_delivery_rows,
)
from .pre_init import EXPECTED_PRE_INIT_EXCEPTIONS, PreInitFailure, run_pre_init_boundary
from .prepare import (
    SpawnCreateArtifacts,
    bind_create_payload_context,
    bind_create_payload_preview,
    build_create_payload,
    validate_create_input,
)
from .query import (
    denied_requests,
    detail_from_row,
    read_spawn_row,
)

_WAIT_PROGRESS_INTERVAL_SECS = 5.0
_WAIT_POLL_INTERVAL_SECS = 0.25


@dataclass(frozen=True)
class _WaitTargetDiscovery:
    blocked_on_ids: tuple[str, ...] = ()
    active_rows: tuple[SpawnRecord, ...] = ()
    owed_rows: tuple[SpawnRecord, ...] = ()
    # Every finished owed id seen, including rows the delivery cap held back.
    owed_candidate_ids: frozenset[str] = frozenset()
    discovery_pending: bool = False


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


def _build_wait_timeout_message(pending_spawn_ids: set[str], elapsed_secs: float) -> str:
    """Build actionable timeout message for LLM agents."""
    sorted_ids = sorted(pending_spawn_ids)
    ids_str = ", ".join(sorted_ids)
    ids_joined = " ".join(sorted_ids)

    lines = [
        f"Wait checkpoint after {elapsed_secs / 60:.0f}m. Still running: {ids_str}",
        "",
        "Check progress:",
    ]
    for spawn_id in sorted_ids[:3]:
        lines.append(f"  meridian session log {spawn_id} --tail")
    if len(sorted_ids) > 3:
        lines.append(f"  ... (+{len(sorted_ids) - 3} more)")

    lines.extend(
        [
            "",
            f"If active, re-wait: meridian spawn wait {ids_joined}",
            f"If stuck, cancel:   meridian spawn cancel {sorted_ids[0]}"
            + (" ..." if len(sorted_ids) > 1 else ""),
        ]
    )

    return "\n".join(lines)


def _resolve_project_root_input(project_root: str | None) -> Path:
    return resolve_project_authority(project_root).project_root


def _project_root_from_prepared(prepared: RuntimeReadContext | RuntimeWriteContext) -> Path:
    return prepared.project_root


def _config_from_prepared(prepared: RuntimeReadContext | RuntimeWriteContext) -> MeridianConfig:
    config = prepared.config
    if config is None:
        raise ValueError("Prepared runtime context is missing config.")
    return config


def _runtime_root_from_prepared_for_read(
    prepared: RuntimeReadContext | RuntimeWriteContext,
    *,
    project_root: Path,
) -> Path:
    runtime_root = prepared.runtime_root
    resolved = runtime_root or resolve_runtime_root_for_read(project_root)
    if resolved is None:
        raise ValueError("Project has no runtime state.")
    return resolved


def _resolve_spawn_read_authority(
    *,
    project_root: str | None,
    prepared: RuntimeReadContext | RuntimeWriteContext | None = None,
) -> tuple[Path, Path | None]:
    if prepared is not None:
        resolved_project_root = _project_root_from_prepared(prepared)
        resolved_runtime_root = prepared.runtime_root
        return resolved_project_root, resolved_runtime_root

    resolved_project_root = _resolve_project_root_input(project_root)
    resolved_runtime_root = resolve_runtime_root_for_read(resolved_project_root)
    return resolved_project_root, resolved_runtime_root


def _surface_primary_activity(status: str, activity: str | None) -> str | None:
    normalized = (activity or "").strip()
    if not normalized:
        return None
    if not is_active_spawn_status(status):
        return None
    return normalized


def _spawn_display_label(row: SpawnRecord) -> str | None:
    return resolve_spawn_display_label(row.goal, row.desc, row.display_label)


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


def _merge_warnings(*warnings: str | None) -> str | None:
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
    """Give the shared pre-init boundary one failure payload shape."""

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


def _run_spawn_creation_boundary(
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


def _run_spawn_create_preparation(
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


def _execute_prepared_spawn_create(
    preparation: SpawnCreatePreparation,
    *,
    sink: OutputSink | None,
    on_spawn_id: Callable[[str], None] | None = None,
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
            warning=_merge_warnings(prepared_request.warning, dry_run_work_warning),
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
        )
    else:
        result = execute_spawn_blocking(
            payload=payload,
            request=prepared_request,
            runtime=runtime,
            ctx=resolved_context,
            prepared=prepared_surface,
            on_spawn_id=on_spawn_id,
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
    floor_check = _run_spawn_creation_boundary(payload)
    if isinstance(floor_check, SpawnActionOutput):
        return floor_check
    preparation = _run_spawn_create_preparation(
        payload,
        ctx=ctx,
        floor_check=floor_check,
        sink=sink,
    )
    if isinstance(preparation, SpawnActionOutput):
        return preparation
    return _execute_prepared_spawn_create(
        preparation,
        sink=sink,
        on_spawn_id=on_spawn_id,
    )


async def spawn_create(
    payload: SpawnCreateInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
) -> SpawnActionOutput:
    return await asyncio.to_thread(
        spawn_create_sync,
        payload,
        ctx=ctx,
        sink=sink,
    )


def spawn_list_sync(
    payload: SpawnListInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnListOutput:
    _ = (ctx, sink)
    if prepared is not None and prepared.runtime_root is None:
        return SpawnListOutput(spawns=())
    _project_root, runtime_root = _resolve_spawn_read_authority(
        project_root=payload.project_root,
        prepared=prepared,
    )
    if runtime_root is None:
        return SpawnListOutput(spawns=())
    spawns = list(
        reversed(overlay_derived(runtime_root, indexed_spawn_scan(runtime_root).records))
    )

    # When statuses is empty tuple, show all statuses but cap intelligently:
    # always include all active spawns, pad with recent non-active up to limit.
    show_all = payload.statuses == ()

    if payload.statuses:
        wanted_statuses = set(payload.statuses)
        spawns = [row for row in spawns if row.status in wanted_statuses]
    elif show_all:
        pass
    elif payload.status is not None:
        spawns = [row for row in spawns if row.status == payload.status]
    else:
        spawns = [row for row in spawns if is_active_spawn_status(row.status)]
    if payload.failed:
        spawns = [row for row in spawns if row.status == "failed"]
    if payload.model is not None and payload.model.strip():
        wanted_model = payload.model.strip()
        spawns = [row for row in spawns if row.model == wanted_model]
    if payload.profile is not None and payload.profile.strip():
        wanted_profile = payload.profile.strip()
        spawns = [row for row in spawns if row.agent == wanted_profile]
    if payload.primary:
        spawns = [row for row in spawns if row.kind == "primary"]

    total_count = len(spawns)
    limit = payload.limit if payload.limit > 0 else 20

    if show_all:
        # Always include all active spawns, fill remaining slots with recent non-active.
        active = [row for row in spawns if is_active_spawn_status(row.status)]
        non_active = [row for row in spawns if not is_active_spawn_status(row.status)]
        effective_limit = max(len(active), limit)
        remaining = effective_limit - len(active)
        selected = active + non_active[:remaining]
    else:
        selected = spawns[:limit]

    truncated = total_count > len(selected)
    entries: list[SpawnListEntry] = []
    for row in selected:
        kind = "primary" if (row.kind or "").strip() == "primary" else None
        managed_backend = False
        activity: str | None = None
        surfaced_activity: str | None = None
        if kind == "primary":
            metadata = read_primary_surface_metadata(runtime_root, row.id)
            managed_backend = metadata.managed_backend
            activity = metadata.activity
            surfaced_activity = _surface_primary_activity(row.status, activity)
        entries.append(
            SpawnListEntry(
                spawn_id=row.id,
                status=row.status,
                status_display=(
                    f"{row.status} ({surfaced_activity})" if surfaced_activity is not None else None
                ),
                model=row.model or "",
                kind=kind,
                activity=surfaced_activity,
                managed_backend=managed_backend,
                duration_secs=row.terminal.duration_secs if row.terminal is not None else None,
                cost_usd=row.terminal.total_cost_usd if row.terminal is not None else None,
            )
        )

    return SpawnListOutput(
        spawns=tuple(entries),
        total_count=total_count if truncated else None,
        truncated=truncated,
    )


async def spawn_list(
    payload: SpawnListInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnListOutput:
    return await asyncio.to_thread(spawn_list_sync, payload, ctx=ctx, sink=sink, prepared=prepared)


def spawn_children_sync(
    payload: SpawnChildrenInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnListOutput:
    _ = sink
    project_root, runtime_root = _resolve_spawn_read_authority(
        project_root=payload.project_root,
        prepared=prepared,
    )
    caller_ref = _resolve_caller_spawn_id(payload.spawn_id, ctx)
    if runtime_root is None:
        return SpawnListOutput(spawns=(), text_view="children")
    spawn_id = resolve_spawn_target(
        project_root,
        caller_ref,
        runtime_root=runtime_root,
    )

    children = list(
        reversed(
            overlay_derived(
                runtime_root,
                indexed_spawn_scan(
                    runtime_root,
                    parent_id=spawn_id,
                ).records,
            )
        )
    )
    entries = tuple(
        SpawnListEntry(
            spawn_id=row.id,
            status=row.status,
            model=row.model or "",
            agent=row.agent or None,
            desc=_spawn_display_label(row),
            duration_secs=row.terminal.duration_secs if row.terminal is not None else None,
            cost_usd=row.terminal.total_cost_usd if row.terminal is not None else None,
        )
        for row in children
    )
    return SpawnListOutput(spawns=entries, text_view="children")


async def spawn_children(
    payload: SpawnChildrenInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnListOutput:
    return await asyncio.to_thread(
        spawn_children_sync,
        payload,
        ctx=ctx,
        sink=sink,
        prepared=prepared,
    )


def spawn_stats_sync(
    payload: SpawnStatsInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnStatsOutput:
    _ = (ctx, sink)
    _project_root, runtime_root = _resolve_spawn_read_authority(
        project_root=payload.project_root,
        prepared=prepared,
    )
    all_spawns = (
        []
        if runtime_root is None
        else overlay_derived(runtime_root, indexed_spawn_scan(runtime_root).records)
    )

    if payload.session is not None and payload.session.strip():
        from meridian.lib.state.session_identity import spawn_matches_exact_session

        wanted_session = payload.session.strip()
        all_spawns = [row for row in all_spawns if spawn_matches_exact_session(row, wanted_session)]

    if payload.spawn_id is not None:
        root_id = payload.spawn_id.strip()
        if payload.flat:
            spawns = [s for s in all_spawns if s.id == root_id]
        else:
            spawns = collect_descendants(root_id, all_spawns)
    else:
        spawns = all_spawns

    model_accum: dict[str, dict[str, int | float]] = {}
    total_duration_secs = 0.0
    total_cost_usd = 0.0
    succeeded = 0
    failed = 0
    cancelled = 0
    timed_out = 0
    running = 0
    finalizing = 0

    for row in spawns:
        if row.status == "succeeded":
            succeeded += 1
        elif row.status == "failed":
            failed += 1
        elif row.status == "cancelled":
            cancelled += 1
        elif row.status == "timed_out":
            timed_out += 1
        elif row.status == "running":
            running += 1
        elif row.status == "finalizing":
            finalizing += 1

        model_key = row.model or ""
        acc = model_accum.setdefault(
            model_key,
            {
                "total": 0,
                "succeeded": 0,
                "failed": 0,
                "cancelled": 0,
                "timed_out": 0,
                "running": 0,
                "finalizing": 0,
                "cost_usd": 0.0,
            },
        )
        acc["total"] = int(acc["total"]) + 1
        if row.status in ALL_SPAWN_STATUSES:
            acc[row.status] = int(acc[row.status]) + 1
        terminal = row.terminal
        if terminal is not None and terminal.total_cost_usd is not None:
            acc["cost_usd"] = float(acc["cost_usd"]) + terminal.total_cost_usd

        if terminal is not None and terminal.duration_secs is not None:
            total_duration_secs += terminal.duration_secs
        if terminal is not None and terminal.total_cost_usd is not None:
            total_cost_usd += terminal.total_cost_usd

    models: dict[str, ModelStats] = {
        k: ModelStats(
            total=int(v["total"]),
            succeeded=int(v["succeeded"]),
            failed=int(v["failed"]),
            cancelled=int(v["cancelled"]),
            timed_out=int(v["timed_out"]),
            running=int(v["running"]),
            finalizing=int(v["finalizing"]),
            cost_usd=float(v["cost_usd"]),
        )
        for k, v in model_accum.items()
    }

    # Build per-child breakdown when scoped to a specific spawn
    children: tuple[SpawnStatsChild, ...] = ()
    if payload.spawn_id is not None and not payload.flat:
        children = tuple(
            SpawnStatsChild(
                spawn_id=s.id,
                status=s.status,
                model=s.model or "",
                duration_secs=s.terminal.duration_secs if s.terminal is not None else None,
                cost_usd=s.terminal.total_cost_usd if s.terminal is not None else None,
                input_tokens=s.terminal.input_tokens if s.terminal is not None else None,
                output_tokens=s.terminal.output_tokens if s.terminal is not None else None,
            )
            for s in spawns
        )

    return SpawnStatsOutput(
        total_runs=len(spawns),
        succeeded=succeeded,
        failed=failed,
        cancelled=cancelled,
        timed_out=timed_out,
        running=running,
        finalizing=finalizing,
        total_duration_secs=total_duration_secs,
        total_cost_usd=total_cost_usd,
        models=models,
        children=children,
    )


async def spawn_stats(
    payload: SpawnStatsInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnStatsOutput:
    return await asyncio.to_thread(spawn_stats_sync, payload, ctx=ctx, sink=sink, prepared=prepared)


def spawn_show_sync(
    payload: SpawnShowInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnDetailOutput:
    _ = sink
    resolved_context = runtime_context(ctx)
    project_root, runtime_root = _resolve_spawn_read_authority(
        project_root=payload.project_root,
        prepared=prepared,
    )
    if runtime_root is None:
        normalized = payload.spawn_id.strip()
        raise ValueError(f"Spawn '{normalized}' not found")
    if prepared is not None:
        spawn_id = resolve_spawn_target(
            project_root,
            payload.spawn_id,
            runtime_root=runtime_root,
        )
    else:
        spawn_id = resolve_spawn_target(project_root, payload.spawn_id)
    row = read_spawn_row(project_root, spawn_id, runtime_root=runtime_root)
    if row is None:
        raise ValueError(f"Spawn '{spawn_id}' not found")
    kind = "primary" if (row.kind or "").strip() == "primary" else None
    managed_backend = False
    activity: str | None = None
    backend_pid: int | None = None
    tui_pid: int | None = None
    backend_port: int | None = None
    harness_session_id: str | None = None
    if kind == "primary":
        metadata = read_primary_surface_metadata(runtime_root, spawn_id)
        managed_backend = metadata.managed_backend
        activity = metadata.activity
        backend_pid = metadata.backend_pid
        tui_pid = metadata.tui_pid
        backend_port = metadata.backend_port
        harness_session_id = metadata.harness_session_id

    detail = detail_from_row(
        project_root=project_root,
        row=row,
        include_report_body=payload.include_report_body,
        runtime_root=runtime_root,
    )
    surfaced_activity = _surface_primary_activity(row.status, activity)
    result = detail.model_copy(
        update={
            "denied_requests": denied_requests(runtime_root, spawn_id),
            "kind": kind,
            "activity": surfaced_activity,
            "managed_backend": managed_backend,
            "backend_pid": backend_pid,
            "tui_pid": tui_pid,
            "backend_port": backend_port,
            "harness_session_id": harness_session_id or detail.harness_session_id,
        }
    )
    consume_result_if_shown(
        observe=payload.observe,
        runtime_root=runtime_root,
        caller=resolved_context,
        spawn_id=result.spawn_id,
        status=result.status,
        body=result.report_body,
    )
    return result


def spawn_status_sync(
    payload: SpawnStatusInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnDetailOutput:
    return spawn_show_sync(
        SpawnShowInput(
            spawn_id=payload.spawn_id,
            include_report_body=payload.include_report_body,
            observe=payload.observe,
            project_root=payload.project_root,
        ),
        ctx=ctx,
        sink=sink,
        prepared=prepared,
    )


async def spawn_show(
    payload: SpawnShowInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnDetailOutput:
    return await asyncio.to_thread(spawn_show_sync, payload, ctx=ctx, sink=sink, prepared=prepared)


async def spawn_status(
    payload: SpawnStatusInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnDetailOutput:
    return await asyncio.to_thread(
        spawn_status_sync, payload, ctx=ctx, sink=sink, prepared=prepared
    )


def spawn_subagents_sync(
    payload: SpawnSubagentsInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnSubagentsOutput:
    _ = sink
    project_root, runtime_root = _resolve_spawn_read_authority(
        project_root=payload.project_root,
        prepared=prepared,
    )
    spawn_id = str(runtime_context(ctx).spawn_id or "").strip() or None
    agent: str | None = None
    if spawn_id is not None and runtime_root is not None:
        row = read_spawn_row(project_root, spawn_id, runtime_root=runtime_root)
        agent = (row.agent or "").strip() or None if row is not None else None

    if agent is not None:
        # Resolved current agent → its declared subagents verbatim.
        # An empty allow-list is a LEAF agent (spawns nothing), not "spawn anything".
        # None means mars could not resolve the profile → treat as empty.
        declared = mars_agent_subagents(project_root, agent)
        names = declared if declared is not None else ()
        inventory = (
            {entry.name: entry for entry in mars_list_subagents(project_root)} if names else {}
        )
        agents = tuple(
            SpawnSubagentEntry(
                name=name,
                scope=inventory[name].scope if name in inventory else None,
            )
            for name in sorted(set(names))
        )
    else:
        # No current agent (no spawn context / record has no agent) → orchestrator
        # view: list all subagent-mode agents.
        agents = tuple(
            SpawnSubagentEntry(name=entry.name, scope=entry.scope)
            for entry in mars_list_subagents(project_root)
        )

    return SpawnSubagentsOutput(agents=agents)


async def spawn_subagents(
    payload: SpawnSubagentsInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnSubagentsOutput:
    return await asyncio.to_thread(
        spawn_subagents_sync,
        payload,
        ctx=ctx,
        sink=sink,
        prepared=prepared,
    )


def _spawn_cancel_output_from_outcome(outcome: CancelOutcome) -> SpawnActionOutput:
    if outcome.status in {"queued", "running"} and not outcome.finalizing:
        return SpawnActionOutput(
            command="spawn.cancel",
            status="failed",
            spawn_id=outcome.spawn_id,
            message=f"Cancellation of spawn '{outcome.spawn_id}' did not converge.",
            error=f"Spawn remained {outcome.status} after cancellation.",
            model=outcome.model,
            harness_id=outcome.harness,
            exit_code=1,
        )
    if outcome.already_terminal:
        message = f"Spawn '{outcome.spawn_id}' is already {outcome.status}."
    elif outcome.finalizing:
        message = (
            "Spawn did not terminate within grace; reaper will reconcile."
            if outcome.status != "cancelled"
            else "Spawn cancelled."
        )
    elif outcome.status == "cancelled":
        message = "Spawn cancelled."
    else:
        message = f"Spawn '{outcome.spawn_id}' is {outcome.status}."

    return SpawnActionOutput(
        command="spawn.cancel",
        status=outcome.status,
        spawn_id=outcome.spawn_id,
        message=message,
        model=outcome.model,
        harness_id=outcome.harness,
        exit_code=outcome.exit_code,
    )


def _normalize_work_filter(work: str | None) -> str | None:
    normalized = (work or "").strip()
    return normalized or None


def _spawn_matches_work_item(
    spawn: SpawnRecord,
    *,
    runtime_root: Path,
    work_id: str,
    active_session_work_ids: dict[str, str] | None = None,
) -> bool:
    normalized_work_id = work_id.strip()
    if not normalized_work_id:
        return False
    if spawn.kind == "primary":
        if active_session_work_ids is None:
            active_session_work_ids = {
                record.chat_id: record.active_work_id
                for record in session_store.list_active_session_records(runtime_root)
                if record.active_work_id is not None and record.active_work_id.strip()
            }
        chat_id = (spawn.chat_id or "").strip()
        return (
            bool(chat_id)
            and is_active_spawn_status(spawn.status)
            and active_session_work_ids.get(chat_id) == normalized_work_id
        )
    return (spawn.work_id or "").strip() == normalized_work_id


def _row_in_cancel_scope(
    row: SpawnRecord,
    *,
    include_others: bool,
    descendant_ids: set[str] | None,
    caller_chat_id: str | None,
) -> bool:
    """Return whether a row falls within the caller's cancel scope.

    include_others=True: unrestricted (all non-primary running spawns).
    descendant_ids set:  nested-spawn caller — only this subtree.
    descendant_ids None: primary/root caller — same chat session.
    """
    if include_others:
        return True
    if descendant_ids is not None:
        return row.id in descendant_ids
    from meridian.lib.state.session_identity import spawn_matches_owner_chat

    return spawn_matches_owner_chat(row, caller_chat_id or "")


def _resolve_caller_spawn_id(spawn_id: str | None, ctx: RuntimeContext | None) -> str:
    """Resolve an explicit spawn id, else the caller's own spawn, else fail."""

    candidate = (spawn_id or "").strip()
    if not candidate:
        candidate = str(runtime_context(ctx).spawn_id or "").strip()
    if not candidate:
        raise ValueError("Spawn ID is required. Pass spawn_id or set MERIDIAN_SPAWN_ID.")
    return candidate


def _resolve_signal_spawn_id(
    *,
    project_root: Path,
    runtime_root: Path,
    spawn_id: str | None,
    ctx: RuntimeContext | None,
) -> str:
    candidate = _resolve_caller_spawn_id(spawn_id, ctx)
    resolved_spawn_id = resolve_spawn_target(
        project_root,
        candidate,
        runtime_root=runtime_root,
    )
    record = spawn_store.get_spawn(runtime_root, resolved_spawn_id)
    if record is None:
        raise ValueError(f"Spawn '{resolved_spawn_id}' not found")
    if record.record_mode == "historical":
        raise ValueError("Historical records have no live process ownership")
    return resolved_spawn_id


def _signal_refusal(
    row: SpawnRecord,
    kind: SpawnSignalKind,
    accepted: frozenset[SpawnSignalKind] | None,
) -> str | None:
    """Return why nothing will consume ``kind`` for this lock-consistent row.

    ``accepted`` is the runner's declared set; ``None`` means it hasn't declared.
    """

    label = f"{row.id} ({row.harness})" if row.harness else row.id
    if row.kind == "primary":
        return (
            f"Spawn {label} is a primary session; `meridian spawn {kind}` applies only "
            "to child spawns."
        )
    if is_terminal_spawn_status(row.status) or row.status == SpawnStatus.FINALIZING:
        return (
            f"Spawn {label} is no longer running (status: {row.status}); "
            f"nothing will read a {kind} signal."
        )
    if accepted is not None and kind not in accepted:
        if not accepted:
            return f"Spawn {label} accepts no completion signals."
        commands = " or ".join(f"`meridian spawn {item}`" for item in sorted(accepted))
        return f"Spawn {label} does not consume {kind} signals; it accepts only {commands}."
    if kind == "rearm":
        snapshot = row.launch_policy_snapshot
        budget = None if snapshot is None else snapshot.execution_policy.resident_rearm_budget
        if budget is not None and row.resident_rearm_count >= budget:
            return (
                f"Spawn {label} has used its rearm budget ({row.resident_rearm_count}/{budget}), "
                "so its completion window can't be extended. Run `meridian spawn done` to finish."
            )
    return None


def _spawn_signal_sync(
    payload: SpawnSignalInput,
    *,
    kind: SpawnSignalKind,
    command: str,
    ctx: RuntimeContext | None = None,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnActionOutput:
    _ = sink
    if prepared is not None:
        project_root = _project_root_from_prepared(prepared)
        runtime_root = _runtime_root_from_prepared_for_read(
            prepared,
            project_root=project_root,
        )
    else:
        project_root, _ = resolve_runtime_root_and_config(payload.project_root)
        runtime_root = resolve_runtime_root(project_root)
    try:
        resolved_spawn_id = _resolve_signal_spawn_id(
            project_root=project_root,
            runtime_root=runtime_root,
            spawn_id=payload.spawn_id,
            ctx=ctx,
        )
        refusal: str | None = None
        reported = False

        def consumer_reads_signal(stored: SpawnRecord) -> bool:
            # Decided under the published-artifact lock that guards the write.
            nonlocal refusal, reported
            row = read_derived(runtime_root, stored.id) or stored
            accepted = read_accepted_signals(runtime_root, stored.id)
            refusal = _signal_refusal(row, kind, accepted)
            reported = accepted is not None
            return refusal is None

        if not write_spawn_signal(
            runtime_root,
            resolved_spawn_id,
            kind,
            can_write=consumer_reads_signal,
        ):
            raise ValueError(refusal or f"Spawn '{resolved_spawn_id}' not found")
    except ValueError as exc:
        return SpawnActionOutput(
            command=command,
            status="failed",
            message=str(exc),
            error=str(exc),
            exit_code=1,
        )
    message = f"Spawn {kind} signal written."
    if not reported:
        message = (
            f"Spawn {kind} signal written; delivery unconfirmed: {resolved_spawn_id} has not "
            "reported which signals it reads (it may still be starting, or its runner "
            "predates signal reporting)."
        )
    return SpawnActionOutput(
        command=command,
        status="succeeded",
        spawn_id=resolved_spawn_id,
        message=message,
    )


def spawn_done_sync(
    payload: SpawnSignalInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnActionOutput:
    return _spawn_signal_sync(
        payload,
        kind="done",
        command="spawn.done",
        ctx=ctx,
        sink=sink,
        prepared=prepared,
    )


def spawn_rearm_sync(
    payload: SpawnSignalInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnActionOutput:
    return _spawn_signal_sync(
        payload,
        kind="rearm",
        command="spawn.rearm",
        ctx=ctx,
        sink=sink,
        prepared=prepared,
    )


async def _spawn_cancel_impl(
    payload: SpawnCancelInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnActionOutput:
    _ = sink
    resolved_context = runtime_context(ctx)
    if prepared is not None:
        project_root = _project_root_from_prepared(prepared)
        runtime_root = _runtime_root_from_prepared_for_read(
            prepared,
            project_root=project_root,
        )
        spawn_service = build_spawn_application_service(prepared)
    else:
        project_root, _ = resolve_runtime_root_and_config(payload.project_root)
        runtime_root = resolve_runtime_root(project_root)
        spawn_service = build_spawn_application_service_from_roots(
            project_root,
            runtime_root,
        )
    if prepared is not None:
        spawn_id = resolve_spawn_target(
            project_root,
            payload.spawn_id,
            runtime_root=runtime_root,
        )
    else:
        spawn_id = resolve_spawn_target(project_root, payload.spawn_id)
    record = spawn_store.get_spawn(runtime_root, spawn_id)
    if record is not None and record.record_mode == "historical":
        raise ValueError("Historical records cannot be cancelled")
    register_debug_trace_observer()
    cancel_owner = str(resolved_context.spawn_id or "cli")
    if prepared is None:
        setup_telemetry(runtime_root=runtime_root, logical_owner=cancel_owner)
    register_spawn_telemetry_observer()
    try:
        outcome = await spawn_service.cancel(SpawnId(spawn_id), force=payload.force)
    except RuntimeError as exc:
        return SpawnActionOutput(
            command="spawn.cancel",
            status="failed",
            spawn_id=spawn_id,
            message=f"Cancel failed: {exc}",
            error=str(exc),
            exit_code=1,
        )
    if payload.observe and outcome.status == "cancelled":
        record_result_shown_to_caller(
            runtime_root=runtime_root,
            caller=resolved_context,
            spawn_id=outcome.spawn_id,
        )
    return _spawn_cancel_output_from_outcome(outcome)


def spawn_cancel_all_sync(
    payload: SpawnCancelAllInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnCancelAllOutput:
    resolved_context = runtime_context(ctx)
    if prepared is not None:
        project_root = _project_root_from_prepared(prepared)
        runtime_root = _runtime_root_from_prepared_for_read(
            prepared,
            project_root=project_root,
        )
    else:
        project_root, _ = resolve_runtime_root_and_config(payload.project_root)
        runtime_root = resolve_runtime_root(project_root)
    work_id = _normalize_work_filter(payload.work)
    caller_chat_id = (resolved_context.chat_id or "").strip() or None
    caller_spawn_id = str(resolved_context.spawn_id) if resolved_context.spawn_id else None
    # Descendant-scope doesn't need chat_id; only raise when we have neither anchor.
    if caller_chat_id is None and caller_spawn_id is None and not payload.include_others:
        raise ValueError(
            "No session context (MERIDIAN_CHAT_ID not set). "
            "Use --include-others to cancel all non-primary running spawns."
        )

    stored_rows = indexed_spawn_scan(runtime_root).records
    if work_id is not None:
        active_session_work_ids: dict[str, str] | None = {
            str(record.chat_id): record.active_work_id
            for record in session_store.list_active_session_records(runtime_root)
            if record.active_work_id is not None and record.active_work_id.strip()
        }
    else:
        active_session_work_ids = None

    # When called from a nested spawn, scope to that spawn's descendants only.
    # When called from a primary/root context, scope to the full chat.
    if caller_spawn_id is not None and not payload.include_others:
        descendant_ids: set[str] | None = descendant_id_set(caller_spawn_id, stored_rows)
    else:
        descendant_ids = None

    target_rows = [
        row
        for row in stored_rows
        if row.status in ACTIVE_SPAWN_STATUSES
        and (
            work_id is None
            or _spawn_matches_work_item(
                row,
                runtime_root=runtime_root,
                work_id=work_id,
                active_session_work_ids=active_session_work_ids,
            )
        )
        and (payload.include_primaries or (row.kind or "").strip() != "primary")
        and _row_in_cancel_scope(
            row,
            include_others=payload.include_others,
            descendant_ids=descendant_ids,
            caller_chat_id=caller_chat_id,
        )
    ]

    results: list[SpawnActionOutput] = []
    for row in target_rows:
        try:
            result = spawn_cancel_sync(
                SpawnCancelInput(
                    spawn_id=row.id,
                    project_root=project_root.as_posix(),
                    force=payload.include_primaries,
                    observe=payload.observe,
                ),
                ctx=resolved_context,
                sink=sink,
                prepared=prepared,
            )
        except ValueError as exc:
            result = SpawnActionOutput(
                command="spawn.cancel",
                status="failed",
                spawn_id=row.id,
                message=f"Cancel failed: {exc}",
                error=str(exc),
                model=row.model,
                harness_id=row.harness,
                exit_code=1,
            )
        results.append(result)

    finalizing_count = sum(1 for result in results if result.status == "finalizing")
    failed_count = sum(1 for result in results if result.status == "failed")
    timed_out_count = sum(1 for result in results if result.status == "timed_out")
    cancelled_count = sum(
        1
        for result in results
        if result.status == "finalizing"
        or (result.status in TERMINAL_SPAWN_STATUSES and result.status == "cancelled")
    )
    return SpawnCancelAllOutput(
        work=work_id,
        total_running=len(target_rows),
        cancelled_count=cancelled_count,
        finalizing_count=finalizing_count,
        failed_count=failed_count,
        timed_out_count=timed_out_count,
        results=tuple(results),
    )


async def spawn_cancel_all(
    payload: SpawnCancelAllInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnCancelAllOutput:
    return await asyncio.to_thread(
        spawn_cancel_all_sync,
        payload,
        ctx=ctx,
        sink=sink,
        prepared=prepared,
    )


def spawn_cancel_sync(
    payload: SpawnCancelInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnActionOutput:
    return asyncio.run(_spawn_cancel_impl(payload, ctx=ctx, sink=sink, prepared=prepared))


async def spawn_cancel(
    payload: SpawnCancelInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnActionOutput:
    return await _spawn_cancel_impl(payload, ctx=ctx, sink=sink, prepared=prepared)


def _spawn_is_terminal(status: str) -> bool:
    return is_terminal_spawn_status(status)


def _resolve_wait_targets(
    payload: SpawnWaitInput,
    runtime_root: Path,
    ctx: RuntimeContext,
    *,
    deadline: float,
    excluded_owed_ids: frozenset[str] = frozenset(),
) -> _WaitTargetDiscovery:
    """Resolve explicit IDs or report that bounded no-arg discovery is unfinished."""
    candidates: list[str] = []
    for spawn_id in payload.spawn_ids:
        normalized = spawn_id.strip()
        if normalized:
            candidates.append(normalized)

    if payload.spawn_id is not None and payload.spawn_id.strip():
        candidates.append(payload.spawn_id.strip())

    if candidates:
        return _WaitTargetDiscovery(blocked_on_ids=tuple(dict.fromkeys(candidates)))

    chat_id = (ctx.chat_id or "").strip()
    if not chat_id:
        raise ValueError(
            "No-arg wait requires MERIDIAN_CHAT_ID (run from inside a meridian session)"
        )

    self_spawn_id = str(ctx.spawn_id) if ctx.spawn_id else None
    try:
        pending = _discover_pending_spawns(
            runtime_root,
            chat_id,
            caller_spawn_id=self_spawn_id,
            deadline=deadline,
        )
    except history_index.HistoryIndexBusy:
        return _WaitTargetDiscovery(discovery_pending=True)

    blocked_on_ids = tuple(row.id for row in pending)
    owed_candidates: tuple[SpawnRecord, ...] = ()
    if self_spawn_id is not None:
        try:
            owed_candidates = resolve_owed_finished_rows(
                runtime_root=runtime_root,
                chat_id=chat_id,
                caller_spawn_id=self_spawn_id,
                excluded_ids=excluded_owed_ids.union(blocked_on_ids),
                deadline=deadline,
            )
        except history_index.HistoryIndexBusy:
            return _WaitTargetDiscovery(discovery_pending=True)

    return _WaitTargetDiscovery(
        blocked_on_ids=blocked_on_ids,
        active_rows=tuple(pending),
        owed_rows=select_delivery_rows(owed_candidates),
        owed_candidate_ids=frozenset(row.id for row in owed_candidates),
    )


def _discover_pending_spawns(
    runtime_root: Path,
    chat_id: str,
    *,
    caller_spawn_id: str | None,
    deadline: float | None = None,
) -> list[SpawnRecord]:
    """Discover active spawns under every parent the caller owns.

    Roots are ``owned_parent_ids``, the same set owed results use: a live primary
    walks the subtrees of every primary generation of its chat, so background work
    started before a continue is found; anyone else walks its own subtree. Peers
    that merely share the chat are not under any root. The index selects membership;
    lifecycle decisions re-read authority. Recursive traversal retains terminal and
    archived ancestry so it cannot lose grandchildren.
    """
    if caller_spawn_id is None:
        return []
    roots = owned_parent_ids(
        runtime_root,
        read_derived(runtime_root, caller_spawn_id),
        chat_id=chat_id,
        caller_spawn_id=caller_spawn_id,
        deadline=deadline,
    )
    if not roots:
        return []
    selected = history_index.HistoryIndex(runtime_root).descendant_projection(
        roots,
        active_only=True,
        deadline=deadline,
    )

    candidates: list[SpawnRecord] = []
    for spawn_id, _parent, _archived in selected:
        if spawn_id == caller_spawn_id:
            continue
        row = spawn_store.get_spawn(runtime_root, spawn_id)
        if row is None:
            # Retirement can race the projection query just as it can race wait polling.
            continue
        candidates.append(row)

    pending = [
        row
        for row in overlay_derived(runtime_root, candidates)
        if row.status in ACTIVE_SPAWN_STATUSES
    ]
    pending.sort(key=lambda row: row.id)
    return pending


def _since_started_spawns(
    *,
    payload: SpawnWaitInput,
    runtime_root: Path,
    project_root: Path,
    ctx: RuntimeContext,
    initial_targets: _WaitTargetDiscovery,
) -> tuple[SpawnDetailOutput, ...]:
    """Discover advisory rows that entered this no-argument wait's scope later.

    "Later" is set membership, not time: anything the wait saw at start (blocked
    on, or finished and owed, even if the delivery cap held it back) is excluded.
    """

    try:
        current_targets = _resolve_wait_targets(
            payload,
            runtime_root,
            ctx,
            deadline=time.monotonic() + history_index.QUERY_TIMEOUT,
            excluded_owed_ids=initial_targets.owed_candidate_ids.union(
                initial_targets.blocked_on_ids
            ),
        )
        if current_targets.discovery_pending:
            return ()
        initial_blocked = frozenset(initial_targets.blocked_on_ids)
        rows = (
            *current_targets.owed_rows,
            *(row for row in current_targets.active_rows if row.id not in initial_blocked),
        )
        return tuple(
            detail_from_row(
                project_root=project_root,
                row=row,
                include_report_body=False,
                runtime_root=runtime_root,
            )
            for row in rows
        )
    # Advisory only, and this wait's `finally` consumes its results either way:
    # any state or index failure here must omit the section, never fail the wait.
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        return ()


def _emit_wait_set(
    spawn_ids: tuple[str, ...],
    project_root: Path,
    *,
    chat_id: str | None = None,
    runtime_root: Path | None = None,
    sink: OutputSink,
) -> None:
    """Print the wait set table before blocking."""
    entries: list[SpawnListEntry] = []
    for spawn_id in spawn_ids:
        row = read_spawn_row(project_root, spawn_id, runtime_root=runtime_root)
        entries.append(
            SpawnListEntry(
                spawn_id=spawn_id,
                agent=row.agent if row else None,
                model=(row.model or "") if row else "",
                status=row.status if row else "unknown",
                duration_secs=None,
                cost_usd=None,
            )
        )

    header = f"Waiting for {len(spawn_ids)} pending spawn(s)"
    if chat_id:
        header += f" for chat {chat_id}"
    header += ":"

    table = "\n".join(
        f"  {line}"
        for line in format_spawn_identity_table(entries, include_duration=False).splitlines()
    )
    sink.status(f"{header}\n{table}")


def _build_wait_multi_output(
    results: tuple[SpawnDetailOutput, ...],
    *,
    fail_fast: bool = False,
    pending_ids: tuple[str, ...] = (),
) -> SpawnWaitMultiOutput:
    total_runs = len(results)
    succeeded_runs = sum(1 for run in results if run.status == "succeeded")
    failed_runs = sum(1 for run in results if run.status == "failed")
    timed_out_runs = sum(1 for run in results if run.status == "timed_out")
    cancelled_runs = sum(1 for run in results if run.status == "cancelled")
    any_failed = any(
        run.status in FAILURE_SPAWN_STATUSES or run.status == "cancelled" for run in results
    )

    spawn_id: str | None = None
    status: str | None = None
    exit_code: int | None = None
    if total_runs == 1:
        spawn_id = results[0].spawn_id
        status = results[0].status
        exit_code = results[0].exit_code

    return SpawnWaitMultiOutput(
        spawns=results,
        total_runs=total_runs,
        succeeded_runs=succeeded_runs,
        failed_runs=failed_runs,
        cancelled_runs=cancelled_runs,
        timed_out_runs=timed_out_runs,
        any_failed=any_failed,
        fail_fast=fail_fast,
        pending_ids=pending_ids,
        spawn_id=spawn_id,
        status=status,
        exit_code=exit_code,
    )


def _build_wait_checkpoint_output(
    details: tuple[SpawnDetailOutput, ...],
    *,
    pending_ids: tuple[str, ...],
    runtime_root: Path,
    chat_id: str | None,
    elapsed_secs: float,
) -> SpawnWaitMultiOutput:
    tree_rows = overlay_derived(
        runtime_root,
        spawn_store.list_spawns(runtime_root).records,
    )
    checkpoint_descendants = {
        spawn_id: tuple(
            row.id
            for row in collect_descendants(spawn_id, tree_rows)
            if row.id != spawn_id and is_active_spawn_status(row.status)
        )
        for spawn_id in pending_ids
    }
    return _build_wait_multi_output(details).model_copy(
        update={
            "checkpoint": True,
            "checkpoint_pending_ids": pending_ids,
            "checkpoint_descendants": {
                spawn_id: descendants
                for spawn_id, descendants in checkpoint_descendants.items()
                if descendants
            },
            "checkpoint_chat_id": chat_id,
            "checkpoint_elapsed_secs": elapsed_secs,
        }
    )


def _resolve_wait_progress_mode(*, verbose: bool, quiet: bool, config_verbosity: str | None) -> str:
    if quiet:
        return "quiet"
    if verbose:
        return "verbose"
    preset = (config_verbosity or "").strip().lower()
    if preset in {"quiet", "verbose", "debug"}:
        return preset
    return "quiet"


def _render_wait_progress(pending: set[str], *, elapsed_secs: float, mode: str) -> str | None:
    if not pending or mode == "quiet":
        return None
    pending_count = len(pending)
    if mode in {"verbose", "debug"}:
        ordered = sorted(pending)
        preview = ", ".join(ordered[:5])
        if len(ordered) > 5:
            preview = f"{preview}, +{len(ordered) - 5} more"
        return f"waiting {elapsed_secs:.1f}s; pending spawns ({pending_count}): {preview}"
    return f"waiting for {pending_count} spawn(s) to finish..."


def _emit_wait_progress(message: str, *, sink: OutputSink) -> None:
    sink.status(message)


def _resolve_wait_checkpoint_seconds(
    *,
    payload: SpawnWaitInput,
    config: MeridianConfig,
) -> float:
    """Resolve per-invocation or harness-aware wait-yield interval."""

    if payload.yield_after_secs is not None:
        return payload.yield_after_secs

    parent_harness = os.getenv("_MERIDIAN_HARNESS")
    return float(config.wait_yield_seconds_for_harness(parent_harness))


def spawn_wait_sync(
    payload: SpawnWaitInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnWaitMultiOutput:
    active_sink = sink or NullSink()
    resolved_context = runtime_context(ctx)
    if prepared is not None:
        project_root = _project_root_from_prepared(prepared)
        config = _config_from_prepared(prepared)
        runtime_root = _runtime_root_from_prepared_for_read(prepared, project_root=project_root)
    else:
        project_root, config = resolve_runtime_root_and_config_for_read(payload.project_root)
        runtime_root = resolve_runtime_root_for_read(project_root)
    if runtime_root is None:
        active_sink.status("No pending spawns.")
        return SpawnWaitMultiOutput()
    has_explicit_ids = bool(payload.spawn_ids) or bool(
        payload.spawn_id is not None and payload.spawn_id.strip()
    )
    wait_chat_id = None if has_explicit_ids else (resolved_context.chat_id or "").strip() or None
    timeout_minutes = (
        payload.timeout if payload.timeout is not None else config.wait_timeout_minutes
    )
    timeout_seconds = timeout_minutes * 60.0
    checkpoint_seconds = _resolve_wait_checkpoint_seconds(payload=payload, config=config)
    started = time.monotonic()
    use_checkpoint = not payload.timeout_explicit
    wait_seconds = checkpoint_seconds if use_checkpoint else timeout_seconds
    wait_deadline = started + max(wait_seconds, 0.0)
    targets = _resolve_wait_targets(
        payload,
        runtime_root,
        resolved_context,
        deadline=min(wait_deadline, time.monotonic() + history_index.QUERY_TIMEOUT),
    )
    if targets.discovery_pending:
        return SpawnWaitMultiOutput(
            checkpoint=True,
            checkpoint_discovery_pending=True,
            checkpoint_chat_id=wait_chat_id,
            checkpoint_elapsed_secs=time.monotonic() - started,
        )
    blocked_on_ids = targets.blocked_on_ids
    owed_rows = targets.owed_rows

    if not blocked_on_ids and not owed_rows:
        chat_display = wait_chat_id or "current chat"
        active_sink.status(f"No pending spawns for chat {chat_display}.")
        return SpawnWaitMultiOutput()

    if has_explicit_ids:
        blocked_on_ids = resolve_spawn_targets(
            project_root,
            blocked_on_ids,
            runtime_root=runtime_root,
        )

    observed_ids = tuple((*blocked_on_ids, *(row.id for row in owed_rows)))

    for spawn_id in observed_ids:
        record = spawn_store.get_spawn(runtime_root, spawn_id)
        if record is not None and record.record_mode == "historical":
            raise ValueError("Historical records cannot be waited on")

    poll = (
        payload.poll_interval_secs
        if payload.poll_interval_secs is not None
        else _WAIT_POLL_INTERVAL_SECS
    )
    if poll <= 0:
        poll = _WAIT_POLL_INTERVAL_SECS

    parent_wait_observer_id = (
        str(resolved_context.spawn_id) if payload.observe and resolved_context.spawn_id else None
    )
    lease_seconds = max(30.0, poll * 2 + 5)
    record_spawn_observation(
        runtime_root=runtime_root,
        parent_spawn_id=parent_wait_observer_id,
        waiting_add=observed_ids,
        lease_seconds=lease_seconds,
    )
    details: tuple[SpawnDetailOutput, ...] = ()
    owed_spawns: tuple[SpawnDetailOutput, ...] = ()
    try:
        owed_spawns = tuple(
            detail_from_row(
                project_root=project_root,
                row=row,
                include_report_body=False,
                runtime_root=runtime_root,
            )
            for row in owed_rows
        )
        if not blocked_on_ids:
            output = _build_wait_multi_output(())
        else:
            _emit_wait_set(
                blocked_on_ids,
                project_root,
                chat_id=wait_chat_id,
                runtime_root=runtime_root,
                sink=active_sink,
            )
            rows: dict[str, SpawnRecord] = {}
            pending: set[str] = set(blocked_on_ids)
            progress_mode = _resolve_wait_progress_mode(
                verbose=payload.verbose,
                quiet=payload.quiet,
                config_verbosity=getattr(getattr(config, "output", None), "verbosity", None),
            )
            progress_interval = max(_WAIT_PROGRESS_INTERVAL_SECS, poll)
            next_progress = started + progress_interval

            while True:
                record_spawn_observation(
                    runtime_root=runtime_root,
                    parent_spawn_id=parent_wait_observer_id,
                    waiting_add=tuple(pending),
                    lease_seconds=lease_seconds,
                )
                for spawn_id in tuple(pending):
                    row = read_spawn_row(project_root, spawn_id, runtime_root=runtime_root)
                    if row is None:
                        if has_explicit_ids:
                            raise ValueError(f"Spawn '{spawn_id}' not found")
                        # No-arg discovery is chat-scoped: if a discovered spawn vanishes while
                        # waiting, treat it as resolved instead of failing unrelated waits.
                        pending.discard(spawn_id)
                        rows.pop(spawn_id, None)
                        continue
                    rows[spawn_id] = row
                    if _spawn_is_terminal(row.status):
                        pending.remove(spawn_id)

                now = time.monotonic()
                failed_fast = payload.fail_fast and any(
                    row.status in FAILURE_SPAWN_STATUSES for row in rows.values()
                )
                if pending and not failed_fast:
                    if now < wait_deadline:
                        if now >= next_progress:
                            progress = _render_wait_progress(
                                pending,
                                elapsed_secs=max(now - started, 0.0),
                                mode=progress_mode,
                            )
                            if progress is not None:
                                _emit_wait_progress(progress, sink=active_sink)
                            next_progress = now + progress_interval
                        time.sleep(min(poll, max(0.0, wait_deadline - time.monotonic())))
                        continue
                    if not use_checkpoint:
                        raise TimeoutError(_build_wait_timeout_message(pending, now - started))

                details = tuple(
                    detail_from_row(
                        project_root=project_root,
                        row=rows[spawn_id],
                        include_report_body=payload.include_report_body,
                        runtime_root=runtime_root,
                    )
                    for spawn_id in blocked_on_ids
                    if spawn_id in rows
                )
                pending_ids = tuple(sorted(pending))
                if not pending_ids:
                    output = _build_wait_multi_output(details)
                elif failed_fast:
                    output = _build_wait_multi_output(
                        details,
                        fail_fast=True,
                        pending_ids=pending_ids,
                    )
                else:
                    output = _build_wait_checkpoint_output(
                        details,
                        pending_ids=pending_ids,
                        runtime_root=runtime_root,
                        chat_id=wait_chat_id,
                        elapsed_secs=now - started,
                    )
                break
        output = output.model_copy(update={"owed_spawns": owed_spawns})
        if not has_explicit_ids:
            output = output.model_copy(
                update={
                    "since_started_spawns": _since_started_spawns(
                        payload=payload,
                        runtime_root=runtime_root,
                        project_root=project_root,
                        ctx=resolved_context,
                        initial_targets=targets,
                    )
                }
            )
        return output
    finally:
        record_spawn_observation(
            runtime_root=runtime_root,
            parent_spawn_id=parent_wait_observer_id,
            waiting_remove=observed_ids,
            observed_add=(
                *(
                    detail.spawn_id
                    for detail in details
                    if _spawn_is_terminal(detail.status)
                ),
                *(row.id for row in owed_rows),
            ),
        )


async def spawn_wait(
    payload: SpawnWaitInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnWaitMultiOutput:
    return await asyncio.to_thread(spawn_wait_sync, payload, ctx=ctx, sink=sink, prepared=prepared)


def _source_spawn_for_follow_up(
    payload_spawn_id: str,
    project_root: Path,
    *,
    runtime_root: Path | None = None,
    harness_hint: str | None = None,
) -> tuple[str, SpawnRecord, ResolvedSessionReference]:
    try:
        # Resolve tracked chats in their native harness first. A requested
        # harness is not evidence that the chat belongs to that harness.
        resolved_reference = resolve_session_reference(
            project_root,
            payload_spawn_id,
            runtime_root=runtime_root,
        )
    except ValueError:
        if harness_hint is None:
            raise
        # An untracked native session ID may need the hint to disambiguate it.
        resolved_reference = resolve_session_reference(
            project_root,
            payload_spawn_id,
            runtime_root=runtime_root,
            harness_hint=harness_hint,
        )
    if (
        harness_hint is not None
        and resolved_reference.harness is not None
        and resolved_reference.harness != harness_hint
    ):
        raise ValueError(
            cross_harness_continue_error(
                resolved_reference.source_chat_id,
                resolved_reference.harness,
                harness_hint,
            )
        )
    # The reference owns which spawn the session came from: a spawn ID names itself,
    # a chat its owner, a native session its recorded provenance.
    resolved_spawn_id = resolved_reference.source_spawn_id
    row = (
        read_spawn_row(project_root, resolved_spawn_id, runtime_root=runtime_root)
        if resolved_spawn_id is not None
        else None
    )
    if resolved_spawn_id is None or row is None:
        raise ValueError(
            f"Spawn '{payload_spawn_id}' not found; continuation requires retained spawn metadata"
        )
    return resolved_spawn_id, row, resolved_reference


def _prompt_for_follow_up(
    source_spawn: SpawnRecord, payload_spawn_id: str, prompt: str | None
) -> str:
    if prompt is not None and prompt.strip():
        return prompt

    existing_prompt = (source_spawn.prompt or "").strip()
    if not existing_prompt:
        raise ValueError(f"Spawn '{payload_spawn_id}' has no stored prompt")
    return existing_prompt


def _reject_continue_policy_overrides(payload: SpawnContinueInput) -> None:
    """Reject launch-contract changes for exact continuation."""

    rejected: list[str] = []
    if payload.skills:
        rejected.append("--skills")
    if payload.approval is not None:
        rejected.append("--approval")
    if payload.sandbox is not None:
        rejected.append("--sandbox")
    if payload.network.mode is not None:
        rejected.append("--network")
    if payload.network.allow:
        rejected.append("--network-allow")
    if payload.network.deny:
        rejected.append("--network-deny")
    if payload.effort is not None:
        rejected.append("--effort")
    if payload.autocompact is not None:
        rejected.append("--autocompact")
    if payload.autocompact_pct is not None:
        rejected.append("--autocompact-pct")
    if payload.passthrough_args:
        rejected.append("--")
    if payload.env:
        rejected.append("--env")
    if payload.work.strip():
        rejected.append("--work")
    if payload.task_dir is not None and payload.task_dir.strip():
        rejected.append("--task-dir")
    if rejected:
        flags = ", ".join(rejected)
        if "--task-dir" in rejected:
            raise ValueError(
                "Cannot use --task-dir with spawn --continue. "
                "Use --fork --task-dir to diverge work location."
            )
        raise ValueError(
            f"Cannot use policy-changing option(s) with spawn --continue: {flags}. "
            "Use --fork-fresh to change launch identity or policy."
        )


def _build_continue_create_input(
    *,
    payload: SpawnContinueInput,
    source_spawn: SpawnRecord,
    source_spawn_id: str,
    resolved_reference: ResolvedSessionReference,
    runtime_root: Path,
) -> SpawnCreateInput:
    continue_contract = build_continue_replay_contract(
        source=continue_replay_source_from_reference(
            source_spawn_id,
            resolved_reference,
            harness_session_id=resolved_reference.authoritative_harness_session_id,
        ),
        explicit_harness=(payload.harness or "").strip() or None,
        requested_agent=payload.agent,
        agent_opt_out=payload.agent_opt_out,
        fork=payload.fork,
        requested_model_override=payload.model,
        runtime_root=runtime_root,
    )
    launch_options = payload.launch_option_updates()
    launch_options.update(
        {
            "harness": continue_contract.harness,
            "passthrough_args": continue_contract.passthrough_args,
        }
    )

    resolved_goal = payload.goal if payload.goal is not None else source_spawn.goal
    derived_prompt = _prompt_for_follow_up(source_spawn, source_spawn_id, payload.prompt)

    return SpawnCreateInput(
        prompt=derived_prompt,
        model=continue_contract.model,
        files=payload.files,
        template_vars=payload.template_vars,
        agent=continue_contract.agent,
        agent_opt_out=continue_contract.agent_opt_out,
        skills=continue_contract.skills,
        goal=resolved_goal,
        desc=payload.desc,
        work=continue_contract.work_id or "",
        task_dir=continue_contract.task_dir,
        caller_cwd=payload.caller_cwd,
        launch_policy_snapshot=continue_contract.launch_policy_snapshot,
        session=continue_contract.session,
        **launch_options,
    )


def _with_command(result: SpawnActionOutput, command: str) -> SpawnActionOutput:
    return result.model_copy(update={"command": command})


def _resolve_fork_agent(
    *,
    payload: SpawnForkInput,
    requested_agent: str | None,
    source_agent: str | None,
) -> str | None:
    if payload.agent_opt_out:
        return None
    if requested_agent is not None:
        return requested_agent
    return source_agent


def _build_fork_create_input(
    *,
    payload: SpawnForkInput,
    normalized_source_ref: str,
    resolved_reference: ResolvedSessionReference,
    requested_model: str,
    requested_agent: str | None,
    inherited_skills: tuple[str, ...],
    requested_work: str,
    requested_task_dir: str | None,
    requested_goal: str | None,
    harness: str | None,
) -> SpawnCreateInput:
    launch_options = payload.launch_option_updates()
    launch_options["harness"] = harness
    return SpawnCreateInput(
        prompt=payload.prompt,
        model=requested_model or (resolved_reference.source_model or ""),
        files=payload.files,
        template_vars=payload.template_vars,
        agent=_resolve_fork_agent(
            payload=payload,
            requested_agent=requested_agent,
            source_agent=resolved_reference.source_agent,
        ),
        agent_opt_out=payload.agent_opt_out,
        skills=inherited_skills,
        desc=payload.desc,
        work=requested_work or (resolved_reference.source_work_id or ""),
        # A fork keeps the source's checkout the way an exact continue does: without
        # it the child silently edits the control root instead of the source's worktree.
        task_dir=requested_task_dir or resolved_reference.source_execution_cwd,
        caller_cwd=payload.caller_cwd,
        goal=requested_goal,
        session=SessionRequest(
            requested_harness_session_id=resolved_reference.authoritative_harness_session_id,
            continue_harness=resolved_reference.harness,
            continue_source_tracked=resolved_reference.tracked,
            continue_source_ref=normalized_source_ref,
            continue_fork=True,
            forked_from_chat_id=resolved_reference.source_chat_id,
            source_control_root=resolved_reference.source_control_root,
            source_execution_cwd=resolved_reference.source_execution_cwd,
            source_native_store=resolved_reference.source_native_store,
        ),
        **launch_options,
    )


def spawn_fork_sync(
    payload: SpawnForkInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
) -> SpawnActionOutput:
    floor_check = _run_spawn_creation_boundary(payload)
    if isinstance(floor_check, SpawnActionOutput):
        return floor_check
    project_root, runtime_root = _resolve_spawn_read_authority(
        project_root=payload.project_root,
    )
    normalized_source_ref = payload.source_ref.strip()
    if not normalized_source_ref:
        raise ValueError("Session reference is required.")
    if runtime_root is None:
        raise ValueError(f"Session reference '{normalized_source_ref}' not found")

    resolved_reference = resolve_session_reference(
        project_root,
        normalized_source_ref,
        runtime_root=runtime_root,
    )
    source_snapshot = resolved_reference.source_launch_policy_snapshot
    source_check = _run_spawn_creation_boundary(
        payload,
        existing_check=floor_check,
        replayed_passthrough_args=(
            source_snapshot.extra_args if source_snapshot is not None else ()
        ),
    )
    if isinstance(source_check, SpawnActionOutput):
        return source_check
    floor_check = source_check
    if resolved_reference.missing_harness_session_id:
        raise NativeSessionUnavailable(normalized_source_ref, "unbound")

    requested_model = payload.model.strip()
    requested_agent = payload.agent
    requested_work = payload.work.strip()
    requested_task_dir = (payload.task_dir or "").strip() or None
    requested_goal = payload.goal if payload.goal is not None else resolved_reference.source_goal
    requested_harness = (payload.harness or "").strip() or None
    source_harness = (resolved_reference.harness or "").strip() or None

    inherited_skills = (
        resolved_reference.source_skills
        if payload.inherit_source_skills and requested_agent is None and not payload.agent_opt_out
        else payload.skills
    )

    unresolved_create_input = _build_fork_create_input(
        payload=payload,
        normalized_source_ref=normalized_source_ref,
        resolved_reference=resolved_reference,
        requested_model=requested_model,
        requested_agent=requested_agent,
        inherited_skills=inherited_skills,
        requested_work=requested_work,
        requested_task_dir=requested_task_dir,
        requested_goal=requested_goal,
        harness=requested_harness,
    )
    create_input = unresolved_create_input.model_copy(
        update={"project_root": project_root.as_posix()},
    )
    preparation = _run_spawn_create_preparation(
        create_input,
        ctx=ctx,
        sink=sink,
        floor_check=floor_check,
    )
    if isinstance(preparation, SpawnActionOutput):
        return preparation
    artifacts = preparation.artifacts
    if artifacts is None:
        raise RuntimeError("Fork preparation did not produce launch artifacts.")
    target_harness = (artifacts.request.harness or "").strip()
    if not target_harness:
        raise ValueError("Fork target harness could not be resolved.")
    if source_harness is not None and source_harness != target_harness:
        raise ValueError(
            "Cannot fork across harnesses: "
            f"source is '{source_harness}', target is '{target_harness}'."
        )

    return _execute_prepared_spawn_create(
        preparation,
        sink=sink,
    )


async def spawn_fork(
    payload: SpawnForkInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
) -> SpawnActionOutput:
    return await asyncio.to_thread(
        spawn_fork_sync,
        payload,
        ctx=ctx,
        sink=sink,
    )


def spawn_continue_sync(
    payload: SpawnContinueInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
) -> SpawnActionOutput:
    floor_check = _run_spawn_creation_boundary(payload)
    if isinstance(floor_check, SpawnActionOutput):
        return _with_command(floor_check, "spawn.continue")
    if payload.model is not None and not payload.model.strip():
        raise ValueError("--model must not be empty")
    project_root, runtime_root = _resolve_spawn_read_authority(
        project_root=payload.project_root,
    )
    if runtime_root is None:
        normalized = payload.spawn_id.strip()
        raise ValueError(f"Spawn '{normalized}' not found")
    resolved_spawn_id, source_spawn, resolved_reference = _source_spawn_for_follow_up(
        payload.spawn_id,
        project_root,
        runtime_root=runtime_root,
        harness_hint=payload.harness,
    )
    source_snapshot = resolved_reference.source_launch_policy_snapshot
    source_check = _run_spawn_creation_boundary(
        payload,
        existing_check=floor_check,
        replayed_passthrough_args=(
            source_snapshot.extra_args if source_snapshot is not None else ()
        ),
    )
    if isinstance(source_check, SpawnActionOutput):
        return _with_command(source_check, "spawn.continue")
    floor_check = source_check
    if resolved_reference.missing_harness_session_id:
        raise NativeSessionUnavailable(resolved_spawn_id, "unbound")

    _reject_continue_policy_overrides(payload)
    create_input = _build_continue_create_input(
        payload=payload,
        source_spawn=source_spawn,
        source_spawn_id=resolved_spawn_id,
        resolved_reference=resolved_reference,
        runtime_root=runtime_root,
    )
    preparation = _run_spawn_create_preparation(
        create_input,
        ctx=ctx,
        sink=sink,
        floor_check=floor_check,
    )
    if isinstance(preparation, SpawnActionOutput):
        return _with_command(preparation, "spawn.continue")
    sweep_before_continue(
        runtime_root,
        resolved_spawn_id,
        chat_id=resolved_reference.source_chat_id,
        dry_run=payload.dry_run,
    )
    result = _execute_prepared_spawn_create(preparation, sink=sink)
    return _with_command(result, "spawn.continue")


async def spawn_continue(
    payload: SpawnContinueInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
) -> SpawnActionOutput:
    return await asyncio.to_thread(
        spawn_continue_sync,
        payload,
        ctx=ctx,
        sink=sink,
    )
