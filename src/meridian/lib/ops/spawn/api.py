"""Spawn operations used by CLI and MCP surfaces."""

import asyncio
import os
import time
from pathlib import Path

from meridian.lib.bootstrap.services import (
    RuntimeReadContext,
    RuntimeWriteContext,
    build_spawn_application_service,
    build_spawn_entrypoint,
)
from meridian.lib.config.settings import MeridianConfig, load_config
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.depth import max_depth_reached
from meridian.lib.core.lifecycle import create_lifecycle_service
from meridian.lib.core.sink import NullSink, OutputSink
from meridian.lib.core.spawn_lifecycle import ACTIVE_SPAWN_STATUSES, is_active_spawn_status
from meridian.lib.core.spawn_service import CancelOutcome, SpawnApplicationService
from meridian.lib.core.telemetry import register_debug_trace_observer
from meridian.lib.core.types import SpawnId
from meridian.lib.launch.request import SessionRequest
from meridian.lib.ops.reference import ResolvedSessionReference, resolve_session_reference
from meridian.lib.ops.runtime import (
    OperationRuntime,
    build_runtime_from_root_and_config,
    resolve_project_authority,
    resolve_runtime_authority_for_read,
    resolve_runtime_authority_for_write,
    resolve_runtime_root,
    resolve_runtime_root_and_config,
    resolve_runtime_root_and_config_for_read,
    resolve_runtime_root_for_read,
    runtime_context,
)
from meridian.lib.ops.work_attachment import ensure_explicit_work_item
from meridian.lib.service_context import SpawnEntryPoint
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_project_paths
from meridian.lib.state.primary_meta import (
    read_primary_surface_metadata,
)
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.telemetry.init import setup_telemetry
from meridian.lib.telemetry.observer import register_spawn_telemetry_observer
from meridian.lib.telemetry.router import emit_telemetry
from meridian.lib.utils.time import minutes_to_seconds

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
    SpawnContinueInput,
    SpawnCreateInput,
    SpawnDetailOutput,
    SpawnListEntry,
    SpawnListInput,
    SpawnListOutput,
    SpawnShowInput,
    SpawnStatsChild,
    SpawnStatsInput,
    SpawnStatsOutput,
    SpawnWaitInput,
    SpawnWaitMultiOutput,
    SpawnWrittenFilesInput,
    SpawnWrittenFilesOutput,
)
from .prepare import build_create_payload, validate_create_input
from .query import (
    detail_from_row,
    read_spawn_row,
    read_written_files,
    resolve_spawn_reference,
    resolve_spawn_references,
)

_WAIT_PROGRESS_INTERVAL_SECS = 5.0


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
        lines.append(f"  meridian session log {spawn_id} --last 5")
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


def _spawn_entrypoint_from_prepared(
    prepared: RuntimeReadContext | RuntimeWriteContext,
) -> SpawnEntryPoint:
    return build_spawn_entrypoint(prepared)


def _project_root_from_entrypoint(entrypoint: SpawnEntryPoint) -> Path:
    project_root = entrypoint.context.project_root
    if project_root is None:
        raise ValueError("Prepared spawn entrypoint is missing project root.")
    return project_root


def _config_from_entrypoint(entrypoint: SpawnEntryPoint) -> MeridianConfig:
    config = entrypoint.context.config
    if config is None:
        raise ValueError("Prepared spawn entrypoint is missing config.")
    return config


def _runtime_root_from_entrypoint_for_read(
    entrypoint: SpawnEntryPoint,
    *,
    project_root: Path,
) -> Path:
    runtime_root = entrypoint.context.runtime_root
    return runtime_root or resolve_runtime_root_for_read(project_root)


def _surface_primary_activity(status: str, activity: str | None) -> str | None:
    normalized = (activity or "").strip()
    if not normalized:
        return None
    if not is_active_spawn_status(status):
        return None
    return normalized


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


def spawn_create_sync(
    payload: SpawnCreateInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnActionOutput:
    prepared_context = prepared
    entrypoint = (
        _spawn_entrypoint_from_prepared(prepared_context)
        if prepared_context is not None
        else None
    )
    register_debug_trace_observer()
    resolved_context = runtime_context(ctx)
    spawn_env_id = os.environ.get("MERIDIAN_SPAWN_ID")
    logical_owner = spawn_env_id if spawn_env_id else "cli"
    authority = None
    if prepared_context is not None:
        assert entrypoint is not None
        resolved_root = _project_root_from_entrypoint(entrypoint)
        config = _config_from_entrypoint(entrypoint)
        authority = prepared_context.authority
        register_spawn_telemetry_observer()
    elif payload.dry_run:
        authority = resolve_runtime_authority_for_read(payload.project_root)
        resolved_root = authority.project_root
        config = load_config(resolved_root, authority=authority)
        setup_telemetry(runtime_root=None, logical_owner=logical_owner)
        register_spawn_telemetry_observer()
    else:
        authority = resolve_runtime_authority_for_write(payload.project_root)
        resolved_root = authority.project_root
        config = load_config(resolved_root, authority=authority)
        setup_telemetry(
            runtime_root=authority.runtime_root,
            logical_owner=logical_owner,
        )
        register_spawn_telemetry_observer()
    payload = payload.model_copy(update={"project_root": resolved_root.as_posix()})
    payload, preflight_warning = validate_create_input(payload)
    if payload.dry_run and payload.work.strip():
        project_local_root = resolve_project_paths(resolved_root).root_dir
        resolved_work_id = ensure_explicit_work_item(project_local_root, payload.work)
        payload = payload.model_copy(update={"work": resolved_work_id})

    runtime = None
    if not payload.dry_run:
        current_depth, max_depth = depth_limits(config.max_depth, ctx=resolved_context)
        if max_depth_reached(current_depth, max_depth):
            return depth_exceeded_output(current_depth, max_depth)
    if prepared_context is not None:
        from meridian.lib.harness.registry import get_default_harness_registry

        runtime = OperationRuntime.from_prepared(
            prepared_context,
            harness_registry=get_default_harness_registry(),
            sink=sink,
        )
    elif not payload.dry_run:
        runtime = build_runtime_from_root_and_config(
            resolved_root,
            config,
            authority=authority,
            sink=sink,
        )

    prepared_request = build_create_payload(
        payload,
        runtime=runtime,
        preflight_warning=preflight_warning,
        ctx=resolved_context,
    )
    forked_from = _forked_from_output(payload)
    if payload.dry_run:
        _emit_usage_spawn_launched(harness=prepared_request.harness)
        return SpawnActionOutput(
            command="spawn.create",
            status="dry-run",
            model=prepared_request.model or "",
            harness_id=prepared_request.harness or "",
            warning=prepared_request.warning,
            agent=prepared_request.agent,
            agent_path=prepared_request.agent_metadata.get("session_agent_path") or None,
            skills=prepared_request.skills,
            skill_paths=prepared_request.skill_paths,
            reference_files=prepared_request.reference_files,
            template_vars=prepared_request.template_vars,
            context_from_resolved=tuple(prepared_request.context_from or ()),
            composed_prompt=prepared_request.prompt,
            model_selection_requested_token=prepared_request.model_selection_requested_token,
            model_selection_canonical_id=prepared_request.model_selection_canonical_id,
            model_selection_harness_provenance=(
                prepared_request.model_selection_harness_provenance
            ),
            project_root=authority.project_root.as_posix(),
            project_root_source=authority.project_root_source,
            runtime_root=authority.runtime_root.as_posix()
            if authority.runtime_root is not None
            else None,
            runtime_root_source=authority.runtime_root_source,
            cli_command=prepared_request.cli_command,
            message="Dry run complete.",
            forked_from=forked_from,
        )

    if runtime is None:
        raise RuntimeError("Spawn runtime was not initialized.")
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
        )
    _emit_usage_spawn_launched(
        harness=result.harness_id or prepared_request.harness,
        spawn_id=result.spawn_id,
    )
    if forked_from is None:
        return result
    return result.model_copy(update={"forked_from": forked_from})


async def spawn_create(
    payload: SpawnCreateInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnActionOutput:
    return await asyncio.to_thread(
        spawn_create_sync,
        payload,
        ctx=ctx,
        sink=sink,
        prepared=prepared,
    )


def spawn_list_sync(
    payload: SpawnListInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnListOutput:
    _ = (ctx, sink)
    entrypoint = _spawn_entrypoint_from_prepared(prepared) if prepared is not None else None
    project_root = (
        _project_root_from_entrypoint(entrypoint)
        if entrypoint is not None
        else _resolve_project_root_input(payload.project_root)
    )
    from meridian.lib.state.reaper import reconcile_spawns

    runtime_root = (
        _runtime_root_from_entrypoint_for_read(entrypoint, project_root=project_root)
        if entrypoint is not None
        else resolve_runtime_root_for_read(project_root)
    )
    spawns = list(
        reversed(
            reconcile_spawns(project_root, runtime_root, spawn_store.list_spawns(runtime_root))
        )
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
                    f"{row.status} ({surfaced_activity})"
                    if surfaced_activity is not None
                    else None
                ),
                model=row.model or "",
                kind=kind,
                activity=surfaced_activity,
                managed_backend=managed_backend,
                duration_secs=row.duration_secs,
                cost_usd=row.total_cost_usd,
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


def _collect_descendants(
    root_id: str,
    all_spawns: list[SpawnRecord],
) -> list[SpawnRecord]:
    """Walk the parent→child tree and return root + all descendants."""
    by_parent: dict[str | None, list[SpawnRecord]] = {}
    for s in all_spawns:
        by_parent.setdefault(s.parent_id, []).append(s)

    result: list[SpawnRecord] = []
    # Find the root spawn itself
    for s in all_spawns:
        if s.id == root_id:
            result.append(s)
            break

    queue = [root_id]
    while queue:
        parent = queue.pop()
        for child in by_parent.get(parent, []):
            result.append(child)
            queue.append(child.id)
    return result


def spawn_stats_sync(
    payload: SpawnStatsInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnStatsOutput:
    _ = (ctx, sink)
    entrypoint = _spawn_entrypoint_from_prepared(prepared) if prepared is not None else None
    project_root = (
        _project_root_from_entrypoint(entrypoint)
        if entrypoint is not None
        else _resolve_project_root_input(payload.project_root)
    )
    from meridian.lib.state.reaper import reconcile_spawns

    runtime_root = (
        _runtime_root_from_entrypoint_for_read(entrypoint, project_root=project_root)
        if entrypoint is not None
        else resolve_runtime_root_for_read(project_root)
    )
    all_spawns = reconcile_spawns(project_root, runtime_root, spawn_store.list_spawns(runtime_root))

    if payload.session is not None and payload.session.strip():
        wanted_session = payload.session.strip()
        all_spawns = [row for row in all_spawns if row.chat_id == wanted_session]

    if payload.spawn_id is not None:
        root_id = payload.spawn_id.strip()
        if payload.flat:
            spawns = [s for s in all_spawns if s.id == root_id]
        else:
            spawns = _collect_descendants(root_id, all_spawns)
    else:
        spawns = all_spawns

    model_accum: dict[str, dict[str, int | float]] = {}
    total_duration_secs = 0.0
    total_cost_usd = 0.0
    succeeded = 0
    failed = 0
    cancelled = 0
    running = 0
    finalizing = 0

    for row in spawns:
        if row.status == "succeeded":
            succeeded += 1
        elif row.status == "failed":
            failed += 1
        elif row.status == "cancelled":
            cancelled += 1
        elif row.status == "running":
            running += 1
        elif row.status == "finalizing":
            finalizing += 1

        model_key = row.model or ""
        acc = model_accum.setdefault(model_key, {
            "total": 0, "succeeded": 0, "failed": 0,
            "cancelled": 0, "running": 0, "finalizing": 0, "cost_usd": 0.0,
        })
        acc["total"] = int(acc["total"]) + 1
        if row.status in ("succeeded", "failed", "cancelled", "running", "finalizing"):
            acc[row.status] = int(acc[row.status]) + 1
        if row.total_cost_usd is not None:
            acc["cost_usd"] = float(acc["cost_usd"]) + row.total_cost_usd

        if row.duration_secs is not None:
            total_duration_secs += row.duration_secs
        if row.total_cost_usd is not None:
            total_cost_usd += row.total_cost_usd

    models: dict[str, ModelStats] = {
        k: ModelStats(
            total=int(v["total"]),
            succeeded=int(v["succeeded"]),
            failed=int(v["failed"]),
            cancelled=int(v["cancelled"]),
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
                duration_secs=s.duration_secs,
                cost_usd=s.total_cost_usd,
                input_tokens=s.input_tokens,
                output_tokens=s.output_tokens,
            )
            for s in spawns
        )

    return SpawnStatsOutput(
        total_runs=len(spawns),
        succeeded=succeeded,
        failed=failed,
        cancelled=cancelled,
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
    _ = (ctx, sink)
    entrypoint = _spawn_entrypoint_from_prepared(prepared) if prepared is not None else None
    project_root = (
        _project_root_from_entrypoint(entrypoint)
        if entrypoint is not None
        else _resolve_project_root_input(payload.project_root)
    )
    runtime_root = (
        _runtime_root_from_entrypoint_for_read(entrypoint, project_root=project_root)
        if entrypoint is not None
        else resolve_runtime_root_for_read(project_root)
    )
    if prepared is not None:
        spawn_id = resolve_spawn_reference(
            project_root,
            payload.spawn_id,
            runtime_root=runtime_root,
        )
    else:
        spawn_id = resolve_spawn_reference(project_root, payload.spawn_id)
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
    return detail.model_copy(
        update={
            "kind": kind,
            "activity": surfaced_activity,
            "managed_backend": managed_backend,
            "backend_pid": backend_pid,
            "tui_pid": tui_pid,
            "backend_port": backend_port,
            "harness_session_id": harness_session_id or detail.harness_session_id,
        }
    )


async def spawn_show(
    payload: SpawnShowInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnDetailOutput:
    return await asyncio.to_thread(spawn_show_sync, payload, ctx=ctx, sink=sink, prepared=prepared)


def spawn_files_sync(
    payload: SpawnWrittenFilesInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnWrittenFilesOutput:
    _ = (ctx, sink)
    entrypoint = _spawn_entrypoint_from_prepared(prepared) if prepared is not None else None
    project_root = (
        _project_root_from_entrypoint(entrypoint)
        if entrypoint is not None
        else _resolve_project_root_input(payload.project_root)
    )
    runtime_root = (
        _runtime_root_from_entrypoint_for_read(entrypoint, project_root=project_root)
        if entrypoint is not None
        else resolve_runtime_root_for_read(project_root)
    )
    if prepared is not None:
        spawn_id = resolve_spawn_reference(
            project_root,
            payload.spawn_id,
            runtime_root=runtime_root,
        )
    else:
        spawn_id = resolve_spawn_reference(project_root, payload.spawn_id)
    row = read_spawn_row(project_root, spawn_id, runtime_root=runtime_root)
    if row is None:
        raise ValueError(f"Spawn '{spawn_id}' not found")
    written_files = read_written_files(project_root, spawn_id, runtime_root=runtime_root)
    return SpawnWrittenFilesOutput(
        spawn_id=spawn_id,
        written_files=written_files,
    )


async def spawn_files(
    payload: SpawnWrittenFilesInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnWrittenFilesOutput:
    return await asyncio.to_thread(spawn_files_sync, payload, ctx=ctx, sink=sink, prepared=prepared)


def _spawn_cancel_output_from_outcome(outcome: CancelOutcome) -> SpawnActionOutput:
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


async def _spawn_cancel_impl(
    payload: SpawnCancelInput,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnActionOutput:
    _ = sink
    entrypoint = _spawn_entrypoint_from_prepared(prepared) if prepared is not None else None
    if prepared is not None:
        assert entrypoint is not None
        project_root = _project_root_from_entrypoint(entrypoint)
        runtime_root = _runtime_root_from_entrypoint_for_read(entrypoint, project_root=project_root)
    else:
        project_root, _ = resolve_runtime_root_and_config(payload.project_root)
        runtime_root = resolve_runtime_root(project_root)
    if prepared is not None:
        spawn_id = resolve_spawn_reference(
            project_root,
            payload.spawn_id,
            runtime_root=runtime_root,
        )
    else:
        spawn_id = resolve_spawn_reference(project_root, payload.spawn_id)
    spawn_service = (
        build_spawn_application_service(prepared)
        if prepared is not None
        else SpawnApplicationService(
            runtime_root,
            create_lifecycle_service(project_root, runtime_root),
        )
    )
    register_debug_trace_observer()
    cancel_owner = os.environ.get("MERIDIAN_SPAWN_ID") or "cli"
    if prepared is None:
        setup_telemetry(runtime_root=runtime_root, logical_owner=cancel_owner)
    register_spawn_telemetry_observer()
    try:
        outcome = await spawn_service.cancel(SpawnId(spawn_id))
    except RuntimeError as exc:
        return SpawnActionOutput(
            command="spawn.cancel",
            status="failed",
            spawn_id=spawn_id,
            message=f"Cancel failed: {exc}",
            error=str(exc),
            exit_code=1,
        )
    return _spawn_cancel_output_from_outcome(outcome)


def spawn_cancel_all_sync(
    payload: SpawnCancelAllInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnCancelAllOutput:
    _ = ctx
    entrypoint = _spawn_entrypoint_from_prepared(prepared) if prepared is not None else None
    if prepared is not None:
        assert entrypoint is not None
        project_root = _project_root_from_entrypoint(entrypoint)
        runtime_root = _runtime_root_from_entrypoint_for_read(entrypoint, project_root=project_root)
    else:
        project_root, _ = resolve_runtime_root_and_config(payload.project_root)
        runtime_root = resolve_runtime_root(project_root)
    work_id = _normalize_work_filter(payload.work)

    from meridian.lib.state.reaper import reconcile_spawns

    active_rows = reconcile_spawns(
        project_root,
        runtime_root,
        spawn_store.list_spawns(runtime_root),
    )
    if work_id is not None:
        active_session_work_ids = {
            record.chat_id: record.active_work_id
            for record in session_store.list_active_session_records(runtime_root)
            if record.active_work_id is not None and record.active_work_id.strip()
        }
    else:
        active_session_work_ids = None

    target_rows = [
        row
        for row in active_rows
        if row.status == "running"
        and (
            work_id is None
            or _spawn_matches_work_item(
                row,
                runtime_root=runtime_root,
                work_id=work_id,
                active_session_work_ids=active_session_work_ids,
            )
        )
    ]

    results: list[SpawnActionOutput] = []
    for row in target_rows:
        try:
            result = spawn_cancel_sync(
                SpawnCancelInput(
                    spawn_id=row.id,
                    project_root=project_root.as_posix(),
                ),
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

    cancelled_count = sum(1 for result in results if result.status == "cancelled")
    failed_count = sum(1 for result in results if result.status == "failed")
    return SpawnCancelAllOutput(
        work=work_id,
        total_running=len(target_rows),
        cancelled_count=cancelled_count,
        failed_count=failed_count,
        results=tuple(results),
    )


async def spawn_cancel_all(
    payload: SpawnCancelAllInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnCancelAllOutput:
    _ = ctx
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
    _ = ctx
    return asyncio.run(_spawn_cancel_impl(payload, sink=sink, prepared=prepared))


async def spawn_cancel(
    payload: SpawnCancelInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnActionOutput:
    _ = ctx
    return await _spawn_cancel_impl(payload, sink=sink, prepared=prepared)


def _spawn_is_terminal(status: str) -> bool:
    return status not in ACTIVE_SPAWN_STATUSES


def _resolve_wait_targets(
    payload: SpawnWaitInput,
    project_root: Path,
    runtime_root: Path,
    ctx: RuntimeContext,
) -> tuple[str, ...]:
    """Resolve explicit wait IDs or discover pending spawns for the current chat."""
    candidates: list[str] = []
    for spawn_id in payload.spawn_ids:
        normalized = spawn_id.strip()
        if normalized:
            candidates.append(normalized)

    if payload.spawn_id is not None and payload.spawn_id.strip():
        candidates.append(payload.spawn_id.strip())

    if candidates:
        return tuple(dict.fromkeys(candidates))

    chat_id = (ctx.chat_id or "").strip()
    if not chat_id:
        raise ValueError(
            "No-arg wait requires MERIDIAN_CHAT_ID "
            "(run from inside a meridian session)"
        )

    self_spawn_id = str(ctx.spawn_id) if ctx.spawn_id else None
    pending = _discover_pending_spawns(
        project_root,
        runtime_root,
        chat_id,
        exclude_spawn_id=self_spawn_id,
    )
    return tuple(row.id for row in pending)


def _discover_pending_spawns(
    project_root: Path,
    runtime_root: Path,
    chat_id: str,
    *,
    exclude_spawn_id: str | None = None,
) -> list[SpawnRecord]:
    """Discover all active spawns for a given chat ID."""
    from meridian.lib.state.reaper import reconcile_spawns

    all_spawns = reconcile_spawns(
        project_root,
        runtime_root,
        spawn_store.list_spawns(runtime_root),
    )
    pending = [
        row
        for row in all_spawns
        if (row.chat_id or "").strip() == chat_id
        and row.status in ACTIVE_SPAWN_STATUSES
        and row.id != exclude_spawn_id
    ]
    pending.sort(key=lambda row: row.id)
    return pending


def _emit_wait_set(
    spawn_ids: tuple[str, ...],
    project_root: Path,
    *,
    chat_id: str | None = None,
    runtime_root: Path | None = None,
    sink: OutputSink,
) -> None:
    """Print the wait set table before blocking."""
    rows: list[tuple[str, str, str]] = []
    for spawn_id in spawn_ids:
        row = read_spawn_row(project_root, spawn_id, runtime_root=runtime_root)
        desc = (row.desc or "").strip() if row else ""
        status = row.status if row else "unknown"
        rows.append((spawn_id, status, desc))

    header = f"Waiting for {len(rows)} pending spawn(s)"
    if chat_id:
        header += f" for chat {chat_id}"
    header += ":"

    lines = [header]
    for spawn_id, status, desc in rows:
        line = f"  {spawn_id}  {status}"
        if desc:
            line += f"  {desc}"
        lines.append(line)

    sink.status("\n".join(lines))


def _build_wait_multi_output(results: tuple[SpawnDetailOutput, ...]) -> SpawnWaitMultiOutput:
    total_runs = len(results)
    succeeded_runs = sum(1 for run in results if run.status == "succeeded")
    failed_runs = sum(1 for run in results if run.status == "failed")
    cancelled_runs = sum(1 for run in results if run.status == "cancelled")
    any_failed = any(run.status in {"failed", "cancelled"} for run in results)

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
        any_failed=any_failed,
        spawn_id=spawn_id,
        status=status,
        exit_code=exit_code,
    )


def _resolve_wait_progress_mode(
    *, verbose: bool, quiet: bool, config_verbosity: str | None
) -> str:
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


def _resolve_wait_yield_after_seconds(
    *,
    payload: SpawnWaitInput,
    spawn_ids: tuple[str, ...],
    project_root: Path,
    config: object,
) -> float:
    """Resolve per-invocation or harness-aware wait-yield interval."""

    if payload.yield_after_secs is not None:
        return payload.yield_after_secs

    resolver = getattr(config, "wait_yield_seconds_for_harness", None)
    if resolver is None:
        return float(getattr(config, "wait_yield_after_seconds", 240.0))

    _ = (spawn_ids, project_root)
    parent_harness = os.getenv("MERIDIAN_HARNESS")
    return float(resolver(parent_harness))


def spawn_wait_sync(
    payload: SpawnWaitInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnWaitMultiOutput:
    active_sink = sink or NullSink()
    resolved_context = runtime_context(ctx)
    entrypoint = _spawn_entrypoint_from_prepared(prepared) if prepared is not None else None
    if prepared is not None:
        assert entrypoint is not None
        project_root = _project_root_from_entrypoint(entrypoint)
        config = _config_from_entrypoint(entrypoint)
        runtime_root = _runtime_root_from_entrypoint_for_read(entrypoint, project_root=project_root)
    else:
        project_root, config = resolve_runtime_root_and_config_for_read(payload.project_root)
        runtime_root = resolve_runtime_root_for_read(project_root)
    has_explicit_ids = bool(payload.spawn_ids) or bool(
        payload.spawn_id is not None and payload.spawn_id.strip()
    )
    spawn_ids = _resolve_wait_targets(payload, project_root, runtime_root, resolved_context)
    wait_chat_id: str | None = None
    if not has_explicit_ids:
        wait_chat_id = (resolved_context.chat_id or "").strip() or None

    if not spawn_ids:
        chat_display = wait_chat_id or "current chat"
        active_sink.status(f"No pending spawns for chat {chat_display}.")
        return SpawnWaitMultiOutput(
            spawns=(),
            total_runs=0,
            succeeded_runs=0,
            failed_runs=0,
            cancelled_runs=0,
            any_failed=False,
        )

    if has_explicit_ids:
        spawn_ids = resolve_spawn_references(project_root, spawn_ids, runtime_root=runtime_root)

    _emit_wait_set(
        spawn_ids,
        project_root,
        chat_id=wait_chat_id,
        runtime_root=runtime_root,
        sink=active_sink,
    )

    timeout_minutes = (
        payload.timeout if payload.timeout is not None else config.wait_timeout_minutes
    )
    timeout_seconds = minutes_to_seconds(timeout_minutes) or 0.0
    checkpoint_seconds = (
        _resolve_wait_yield_after_seconds(
            payload=payload,
            spawn_ids=spawn_ids,
            project_root=project_root,
            config=config,
        )
    )
    started = time.monotonic()
    use_checkpoint = not payload.timeout_explicit
    if use_checkpoint:
        checkpoint_deadline = started + max(checkpoint_seconds, 0.0)
        hard_deadline = None
    else:
        checkpoint_deadline = None
        hard_deadline = started + max(timeout_seconds, 0.0)
    poll = (
        payload.poll_interval_secs
        if payload.poll_interval_secs is not None
        else config.retry_backoff_seconds
    )
    if poll <= 0:
        poll = config.retry_backoff_seconds

    completed_rows: dict[str, SpawnRecord] = {}
    pending: set[str] = set(spawn_ids)
    progress_mode = _resolve_wait_progress_mode(
        verbose=payload.verbose,
        quiet=payload.quiet,
        config_verbosity=getattr(getattr(config, "output", None), "verbosity", None),
    )
    progress_interval = max(_WAIT_PROGRESS_INTERVAL_SECS, poll)
    next_progress = started + progress_interval

    while True:
        for spawn_id in tuple(pending):
            row = read_spawn_row(project_root, spawn_id, runtime_root=runtime_root)
            if row is None:
                if has_explicit_ids:
                    raise ValueError(f"Spawn '{spawn_id}' not found")
                # No-arg discovery is chat-scoped: if a discovered spawn vanishes while
                # waiting, treat it as resolved instead of failing unrelated waits.
                pending.discard(spawn_id)
                continue

            if _spawn_is_terminal(row.status):
                completed_rows[spawn_id] = row
                pending.remove(spawn_id)

        if not pending:
            details = tuple(
                detail_from_row(
                    project_root=project_root,
                    row=completed_rows[spawn_id],
                    include_report_body=payload.include_report_body,
                    runtime_root=runtime_root,
                )
                for spawn_id in spawn_ids
                if spawn_id in completed_rows
            )
            return _build_wait_multi_output(details)

        now = time.monotonic()
        if checkpoint_deadline is not None and now >= checkpoint_deadline:
            pending_ids = tuple(sorted(pending))
            checkpoint_rows: list[SpawnRecord] = []
            for spawn_id in spawn_ids:
                if spawn_id in completed_rows:
                    checkpoint_rows.append(completed_rows[spawn_id])
                    continue
                row = read_spawn_row(project_root, spawn_id, runtime_root=runtime_root)
                if row is not None:
                    checkpoint_rows.append(row)
            checkpoint_details = tuple(
                detail_from_row(
                    project_root=project_root,
                    row=row,
                    include_report_body=payload.include_report_body,
                    runtime_root=runtime_root,
                )
                for row in checkpoint_rows
            )
            return SpawnWaitMultiOutput(
                spawns=checkpoint_details,
                total_runs=len(spawn_ids),
                succeeded_runs=sum(
                    1 for detail in checkpoint_details if detail.status == "succeeded"
                ),
                failed_runs=sum(1 for detail in checkpoint_details if detail.status == "failed"),
                cancelled_runs=sum(
                    1 for detail in checkpoint_details if detail.status == "cancelled"
                ),
                any_failed=any(
                    detail.status in {"failed", "cancelled"} for detail in checkpoint_details
                ),
                checkpoint=True,
                checkpoint_pending_ids=pending_ids,
                checkpoint_chat_id=wait_chat_id,
                checkpoint_elapsed_secs=now - started,
            )

        if hard_deadline is not None and now >= hard_deadline:
            elapsed = now - started
            raise TimeoutError(_build_wait_timeout_message(pending, elapsed))
        if now >= next_progress:
            progress = _render_wait_progress(
                pending,
                elapsed_secs=max(now - started, 0.0),
                mode=progress_mode,
            )
            if progress is not None:
                _emit_wait_progress(progress, sink=active_sink)
            next_progress = now + progress_interval
        time.sleep(poll)


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
) -> tuple[str, SpawnRecord, ResolvedSessionReference]:
    resolved_spawn_id = resolve_spawn_reference(
        project_root,
        payload_spawn_id,
        runtime_root=runtime_root,
    )
    row = read_spawn_row(project_root, resolved_spawn_id, runtime_root=runtime_root)
    if row is None:
        raise ValueError(f"Spawn '{resolved_spawn_id}' not found")
    resolved_reference = resolve_session_reference(
        project_root,
        resolved_spawn_id,
        runtime_root=runtime_root,
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


def _model_for_follow_up(source_spawn: SpawnRecord, override_model: str) -> str:
    if override_model.strip():
        return override_model
    return (source_spawn.model or "").strip()


def _with_command(result: SpawnActionOutput, command: str) -> SpawnActionOutput:
    return result.model_copy(update={"command": command})


def spawn_continue_sync(
    payload: SpawnContinueInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnActionOutput:
    if prepared is not None:
        entrypoint = _spawn_entrypoint_from_prepared(prepared)
        project_root = _project_root_from_entrypoint(entrypoint)
        runtime_root = _runtime_root_from_entrypoint_for_read(entrypoint, project_root=project_root)
    else:
        project_root, _ = resolve_runtime_root_and_config(payload.project_root)
        runtime_root = None
    resolved_spawn_id, source_spawn, resolved_reference = _source_spawn_for_follow_up(
        payload.spawn_id,
        project_root,
        runtime_root=runtime_root,
    )
    if resolved_reference.missing_harness_session_id:
        raise ValueError(
            f"Spawn '{resolved_spawn_id}' has no recorded session — cannot continue/fork."
        )

    requested_harness = (payload.harness or "").strip() or None
    source_harness = (resolved_reference.harness or "").strip() or None
    if (
        requested_harness is not None
        and source_harness is not None
        and requested_harness != source_harness
    ):
        raise ValueError(
            f"Cannot continue spawn '{resolved_spawn_id}' with harness '{requested_harness}'; "
            f"source spawn uses '{source_harness}'."
        )

    derived_prompt = _prompt_for_follow_up(source_spawn, resolved_spawn_id, payload.prompt)
    create_input = SpawnCreateInput(
        prompt=derived_prompt,
        model=_model_for_follow_up(source_spawn, payload.model),
        harness=requested_harness,
        agent=payload.agent,
        skills=payload.skills,
        project_root=payload.project_root,
        dry_run=payload.dry_run,
        timeout=payload.timeout,
        background=payload.background,
        session=SessionRequest(
            requested_harness_session_id=resolved_reference.harness_session_id,
            continue_harness=resolved_reference.harness,
            continue_source_tracked=resolved_reference.tracked,
            continue_source_ref=resolved_spawn_id,
            continue_fork=payload.fork,
            continue_chat_id=resolved_reference.source_chat_id,
            forked_from_chat_id=resolved_reference.source_chat_id if payload.fork else None,
            source_execution_cwd=resolved_reference.source_execution_cwd,
            source_claude_config_dir=resolved_reference.source_claude_config_dir,
        ),
        passthrough_args=payload.passthrough_args,
        approval=payload.approval,
    )
    if prepared is not None:
        result = spawn_create_sync(create_input, ctx=ctx, sink=sink, prepared=prepared)
    else:
        result = spawn_create_sync(create_input, ctx=ctx, sink=sink)
    return _with_command(result, "spawn.continue")


async def spawn_continue(
    payload: SpawnContinueInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnActionOutput:
    return await asyncio.to_thread(
        spawn_continue_sync,
        payload,
        ctx=ctx,
        sink=sink,
        prepared=prepared,
    )
