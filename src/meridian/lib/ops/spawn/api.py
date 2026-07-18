"""Spawn operations used by CLI and MCP surfaces."""

import asyncio
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from meridian.lib.bootstrap.services import (
    RuntimeReadContext,
    RuntimeWriteContext,
    build_spawn_application_service,
    build_spawn_application_service_from_roots,
)
from meridian.lib.config.settings import MeridianConfig, load_config
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.depth import max_depth_reached
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
from meridian.lib.launch.continue_replay import (
    build_continue_replay_contract,
    continue_replay_source_from_reference,
)
from meridian.lib.launch.request import SessionRequest
from meridian.lib.ops.mars import mars_agent_subagents, mars_list_subagents
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
from meridian.lib.platform.locking import lock_file
from meridian.lib.state import session_store, spawn_store, work_store
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.paths import resolve_project_paths
from meridian.lib.state.primary_meta import (
    read_primary_surface_metadata,
)
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_signals import SpawnSignalKind, write_spawn_signal
from meridian.lib.state.spawn_tree import collect_descendants, descendant_id_set
from meridian.lib.state.work_state import slugify
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
    SpawnSubagentsInput,
    SpawnSubagentsOutput,
    SpawnWaitInput,
    SpawnWaitMultiOutput,
    SpawnWrittenFilesInput,
    SpawnWrittenFilesOutput,
)
from .pre_init import EXPECTED_PRE_INIT_EXCEPTIONS, PreInitFailure, run_pre_init_boundary
from .prepare import SpawnCreateArtifacts, build_create_payload, validate_create_input
from .query import (
    detail_from_row,
    read_spawn_row,
    read_written_files,
    resolve_spawn_reference,
    resolve_spawn_references,
)

_WAIT_PROGRESS_INTERVAL_SECS = 5.0


def _looks_like_spawn_ref(ref: str) -> bool:
    normalized = ref.strip()
    return len(normalized) > 1 and normalized[0] == "p" and normalized[1:].isdigit()


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
    return runtime_root or resolve_runtime_root_for_read(project_root)


def _resolve_spawn_read_authority(
    *,
    project_root: str | None,
    prepared: RuntimeReadContext | RuntimeWriteContext | None = None,
) -> tuple[Path, Path]:
    if prepared is not None:
        resolved_project_root = _project_root_from_prepared(prepared)
        resolved_runtime_root = _runtime_root_from_prepared_for_read(
            prepared,
            project_root=resolved_project_root,
        )
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


def _missing_follow_up_session_error(source_ref: str) -> str:
    normalized = source_ref.strip()
    if normalized.startswith("p") and normalized[1:].isdigit():
        return f"Spawn '{normalized}' has no recorded session — cannot continue/fork."
    return f"Session '{normalized}' has no recorded harness session — cannot continue/fork."


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
    runtime: OperationRuntime | None
    artifacts: SpawnCreateArtifacts | None
    dry_run_work_warning: str | None
    depth_exceeded: SpawnActionOutput | None = None


def _prepare_spawn_create(
    *,
    payload: SpawnCreateInput,
    ctx: RuntimeContext | None,
    sink: OutputSink | None,
    prepared: RuntimeWriteContext | None,
    failure_payload: list[SpawnCreateInput],
) -> SpawnCreatePreparation:
    prepared_context = prepared
    register_debug_trace_observer()
    resolved_context = runtime_context(ctx)
    spawn_env_id = os.environ.get("MERIDIAN_SPAWN_ID")
    logical_owner = spawn_env_id if spawn_env_id else "cli"
    authority = None
    if prepared_context is not None:
        resolved_root = _project_root_from_prepared(prepared_context)
        config = _config_from_prepared(prepared_context)
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

    runtime = None
    if not payload.dry_run:
        current_depth, max_depth = depth_limits(config.max_depth, ctx=resolved_context)
        if max_depth_reached(current_depth, max_depth):
            return SpawnCreatePreparation(
                payload=payload,
                resolved_context=resolved_context,
                authority=authority,
                runtime=None,
                artifacts=None,
                dry_run_work_warning=dry_run_work_warning,
                depth_exceeded=depth_exceeded_output(current_depth, max_depth),
            )
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

    artifacts = build_create_payload(
        payload,
        runtime=runtime,
        preflight_warning=preflight_warning,
        ctx=resolved_context,
    )
    return SpawnCreatePreparation(
        payload=payload,
        resolved_context=resolved_context,
        authority=authority,
        runtime=runtime,
        artifacts=artifacts,
        dry_run_work_warning=dry_run_work_warning,
    )


def _prepare_spawn_create_with_expected_failures(
    *,
    payload: SpawnCreateInput,
    ctx: RuntimeContext | None,
    sink: OutputSink | None,
    prepared: RuntimeWriteContext | None,
    failure_payload: list[SpawnCreateInput],
) -> SpawnCreatePreparation:
    try:
        return _prepare_spawn_create(
            payload=payload,
            ctx=ctx,
            sink=sink,
            prepared=prepared,
            failure_payload=failure_payload,
        )
    except (*EXPECTED_PRE_INIT_EXCEPTIONS, RuntimeError) as exc:
        raise PreInitFailure(str(exc)) from exc


def spawn_create_sync(
    payload: SpawnCreateInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
    on_spawn_id: Callable[[str], None] | None = None,
) -> SpawnActionOutput:
    failure_payload = [payload]
    preparation = run_pre_init_boundary(
        payload=lambda: failure_payload[0],
        operation=lambda: _prepare_spawn_create_with_expected_failures(
            payload=payload,
            ctx=ctx,
            sink=sink,
            prepared=prepared,
            failure_payload=failure_payload,
        ),
    )
    if isinstance(preparation, SpawnActionOutput):
        return preparation
    if preparation.depth_exceeded is not None:
        return preparation.depth_exceeded

    payload = preparation.payload
    resolved_context = preparation.resolved_context
    authority = preparation.authority
    runtime = preparation.runtime
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
            context_from_resolved=tuple(prepared_request.context_from or ()),
            composed_prompt=prepared_request.prompt,
            goal=prepared_goal,
            model_selection_requested_token=prepared_request.model_selection_requested_token,
            model_selection_canonical_id=prepared_request.model_selection_canonical_id,
            model_selection_harness_provenance=(
                prepared_request.model_selection_harness_provenance
            ),
            matched_policy_rule=getattr(prepared_request, "matched_policy_rule", None),
            fallback_chain=tuple(getattr(prepared_request, "fallback_chain", ()) or ()),
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
            prepared=prepared_surface,
            on_spawn_id=on_spawn_id,
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
    project_root, runtime_root = _resolve_spawn_read_authority(
        project_root=payload.project_root,
        prepared=prepared,
    )
    from meridian.lib.state.reaper import reconcile_spawns

    spawns = list(
        reversed(
            reconcile_spawns(
                project_root, runtime_root, spawn_store.list_spawns(runtime_root)
            ).records
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
    _ = (ctx, sink)
    project_root, runtime_root = _resolve_spawn_read_authority(
        project_root=payload.project_root,
        prepared=prepared,
    )
    normalized_ref = payload.spawn_id.strip()
    if not normalized_ref:
        raise ValueError("spawn_id is required")
    spawn_id = resolve_spawn_reference(
        project_root,
        normalized_ref,
        runtime_root=runtime_root,
    )

    from meridian.lib.state.reaper import reconcile_spawns

    children = list(
        reversed(
            reconcile_spawns(
                project_root,
                runtime_root,
                spawn_store.list_spawns(
                    runtime_root,
                    parent_id=spawn_id,
                ),
            ).records
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
    project_root, runtime_root = _resolve_spawn_read_authority(
        project_root=payload.project_root,
        prepared=prepared,
    )
    from meridian.lib.state.reaper import reconcile_spawns

    all_spawns = list(
        reconcile_spawns(
            project_root, runtime_root, spawn_store.list_spawns(runtime_root)
        ).records
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
    _ = (ctx, sink)
    project_root, runtime_root = _resolve_spawn_read_authority(
        project_root=payload.project_root,
        prepared=prepared,
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


def spawn_files_sync(
    payload: SpawnWrittenFilesInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnWrittenFilesOutput:
    _ = (ctx, sink)
    project_root, runtime_root = _resolve_spawn_read_authority(
        project_root=payload.project_root,
        prepared=prepared,
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


def spawn_subagents_sync(
    payload: SpawnSubagentsInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnSubagentsOutput:
    _ = (ctx, sink)
    project_root, runtime_root = _resolve_spawn_read_authority(
        project_root=payload.project_root,
        prepared=prepared,
    )
    spawn_id = (os.environ.get("MERIDIAN_SPAWN_ID") or "").strip() or None
    agent: str | None = None
    if spawn_id is not None:
        row = read_spawn_row(project_root, spawn_id, runtime_root=runtime_root)
        agent = (row.agent or "").strip() or None if row is not None else None

    if agent is not None:
        # Resolved current agent → its declared subagents verbatim.
        # An empty allow-list is a LEAF agent (spawns nothing), not "spawn anything".
        # None means mars could not resolve the profile → treat as empty.
        declared = mars_agent_subagents(project_root, agent)
        names = declared if declared is not None else ()
    else:
        # No current agent (no spawn context / record has no agent) → orchestrator
        # view: list all subagent-mode agents.
        names = mars_list_subagents(project_root)

    return SpawnSubagentsOutput(names=tuple(sorted(set(names))))


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


def _resolve_signal_spawn_id(
    *,
    project_root: Path,
    runtime_root: Path,
    spawn_id: str | None,
    ctx: RuntimeContext | None,
) -> str:
    candidate = (spawn_id or "").strip()
    if not candidate:
        candidate = str(runtime_context(ctx).spawn_id or "").strip()
    if not candidate:
        raise ValueError("Spawn ID is required. Pass spawn_id or set MERIDIAN_SPAWN_ID.")
    resolved_spawn_id = resolve_spawn_reference(
        project_root,
        candidate,
        runtime_root=runtime_root,
    )
    if spawn_store.get_spawn(runtime_root, resolved_spawn_id) is None:
        raise ValueError(f"Spawn '{resolved_spawn_id}' not found")
    return resolved_spawn_id


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
        if not write_spawn_signal(runtime_root, resolved_spawn_id, kind):
            raise ValueError(f"Spawn '{resolved_spawn_id}' not found")
    except ValueError as exc:
        return SpawnActionOutput(
            command=command,
            status="failed",
            message=str(exc),
            error=str(exc),
            exit_code=1,
        )
    return SpawnActionOutput(
        command=command,
        status="succeeded",
        spawn_id=resolved_spawn_id,
        message=f"Spawn {kind} signal written.",
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
    *,
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
        spawn_service = build_spawn_application_service(prepared)
    else:
        project_root, _ = resolve_runtime_root_and_config(payload.project_root)
        runtime_root = resolve_runtime_root(project_root)
        spawn_service = build_spawn_application_service_from_roots(
            project_root,
            runtime_root,
        )
    if prepared is not None:
        spawn_id = resolve_spawn_reference(
            project_root,
            payload.spawn_id,
            runtime_root=runtime_root,
        )
    else:
        spawn_id = resolve_spawn_reference(project_root, payload.spawn_id)
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

    from meridian.lib.state.reaper import reconcile_spawns

    active_rows = reconcile_spawns(
        project_root,
        runtime_root,
        spawn_store.list_spawns(runtime_root),
    ).records
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
        descendant_ids: set[str] | None = descendant_id_set(caller_spawn_id, active_rows)
    else:
        descendant_ids = None

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
    return is_terminal_spawn_status(status)


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
            "No-arg wait requires MERIDIAN_CHAT_ID (run from inside a meridian session)"
        )

    self_spawn_id = str(ctx.spawn_id) if ctx.spawn_id else None
    pending = _discover_pending_spawns(
        project_root,
        runtime_root,
        chat_id,
        exclude_spawn_id=self_spawn_id,
        only_descendants_of=self_spawn_id,
    )
    return tuple(row.id for row in pending)


def _discover_pending_spawns(
    project_root: Path,
    runtime_root: Path,
    chat_id: str,
    *,
    exclude_spawn_id: str | None = None,
    only_descendants_of: str | None = None,
) -> list[SpawnRecord]:
    """Discover active spawns for a given chat ID.

    When *only_descendants_of* is set (i.e. called from a nested spawn),
    returns only spawns that are descendants of that spawn — not siblings,
    ancestors, or the primary session. This prevents no-arg ``spawn wait``
    from blocking on the entire chat tree.
    """
    from meridian.lib.state.reaper import reconcile_spawns
    from meridian.lib.state.session_identity import spawn_matches_owner_chat

    all_spawns = reconcile_spawns(
        project_root,
        runtime_root,
        spawn_store.list_spawns(runtime_root),
    ).records

    # Build descendant set if scoping to a parent
    descendant_ids: set[str] | None = None
    if only_descendants_of is not None:
        descendant_ids = descendant_id_set(only_descendants_of, all_spawns)

    pending = [
        row
        for row in all_spawns
        if row.status in ACTIVE_SPAWN_STATUSES
        and row.id != exclude_spawn_id
        and (descendant_ids is None or row.id in descendant_ids)
        and (descendant_ids is not None or spawn_matches_owner_chat(row, chat_id or ""))
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
        spawn_id=spawn_id,
        status=status,
        exit_code=exit_code,
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
    spawn_ids: tuple[str, ...],
    project_root: Path,
    config: MeridianConfig,
) -> float:
    """Resolve per-invocation or harness-aware wait-yield interval."""

    if payload.yield_after_secs is not None:
        return payload.yield_after_secs

    _ = (spawn_ids, project_root)
    parent_harness = os.getenv("MERIDIAN_HARNESS")
    return float(config.wait_yield_seconds_for_harness(parent_harness))


def _update_pi_wait_observation(
    *,
    runtime_root: Path,
    parent_spawn_id: str | None,
    waiting_add: tuple[str, ...] = (),
    waiting_remove: tuple[str, ...] = (),
    observed_add: tuple[str, ...] = (),
) -> None:
    """Record spawn IDs the parent session is explicitly waiting for or saw.

    The Pi spawn watcher uses this as a suppression marker for implicit
    completion notifications. ``state.json`` remains authority for spawn state;
    this file is UI policy state under the parent Pi session's coordination dir.
    """

    if not parent_spawn_id or not (waiting_add or waiting_remove or observed_add):
        return

    observed_path = runtime_root / "pi-bash" / parent_spawn_id / "observed-spawns.json"
    lock_path = runtime_root / "pi-bash" / parent_spawn_id / "observed-spawns.lock"
    with lock_file(lock_path):
        try:
            existing = json.loads(observed_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            existing = {}
        existing_obj = cast("dict[str, object]", existing) if isinstance(existing, dict) else {}
        observed_ids = _string_set(existing_obj.get("observed_spawn_ids"))
        waiting_ids = _string_set(existing_obj.get("waiting_spawn_ids"))
        waiting_ids.update(waiting_add)
        waiting_ids.difference_update(waiting_remove)
        observed_ids.update(observed_add)
        observed_ids.difference_update(waiting_ids)
        atomic_write_text(
            observed_path,
            json.dumps(
                {
                    "v": 1,
                    "spawn_id": parent_spawn_id,
                    "updated_at_ms": int(time.time() * 1000),
                    "observed_spawn_ids": sorted(observed_ids),
                    "waiting_spawn_ids": sorted(waiting_ids),
                },
                indent=2,
            )
            + "\n",
        )


def _string_set(raw: object) -> set[str]:
    if not isinstance(raw, list):
        return set()
    return {item for item in cast("list[object]", raw) if isinstance(item, str)}


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
    checkpoint_seconds = _resolve_wait_checkpoint_seconds(
        payload=payload,
        spawn_ids=spawn_ids,
        project_root=project_root,
        config=config,
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

    parent_wait_observer_id = str(resolved_context.spawn_id) if resolved_context.spawn_id else None
    _update_pi_wait_observation(
        runtime_root=runtime_root,
        parent_spawn_id=parent_wait_observer_id,
        waiting_add=spawn_ids,
    )
    try:
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
                _update_pi_wait_observation(
                    runtime_root=runtime_root,
                    parent_spawn_id=parent_wait_observer_id,
                    waiting_remove=spawn_ids,
                    observed_add=tuple(detail.spawn_id for detail in details),
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
                _update_pi_wait_observation(
                    runtime_root=runtime_root,
                    parent_spawn_id=parent_wait_observer_id,
                    waiting_remove=spawn_ids,
                    observed_add=tuple(
                        detail.spawn_id
                        for detail in checkpoint_details
                        if _spawn_is_terminal(detail.status)
                    ),
                )
                return SpawnWaitMultiOutput(
                    spawns=checkpoint_details,
                    total_runs=len(spawn_ids),
                    succeeded_runs=sum(
                        1 for detail in checkpoint_details if detail.status == "succeeded"
                    ),
                    failed_runs=sum(
                        1 for detail in checkpoint_details if detail.status == "failed"
                    ),
                    cancelled_runs=sum(
                        1 for detail in checkpoint_details if detail.status == "cancelled"
                    ),
                    timed_out_runs=sum(
                        1 for detail in checkpoint_details if detail.status == "timed_out"
                    ),
                    any_failed=any(
                        detail.status in FAILURE_SPAWN_STATUSES or detail.status == "cancelled"
                        for detail in checkpoint_details
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
    finally:
        _update_pi_wait_observation(
            runtime_root=runtime_root,
            parent_spawn_id=parent_wait_observer_id,
            waiting_remove=spawn_ids,
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


def _reject_continue_policy_overrides(payload: SpawnContinueInput) -> None:
    """Reject launch-contract changes for exact continuation."""

    rejected: list[str] = []
    if payload.model.strip():
        rejected.append("--model")
    if payload.skills:
        rejected.append("--skills")
    if payload.approval is not None:
        rejected.append("--approval")
    if payload.sandbox is not None:
        rejected.append("--sandbox")
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
        task_dir=requested_task_dir,
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
            source_claude_config_dir=resolved_reference.source_claude_config_dir,
            source_pi_session_dir=resolved_reference.source_pi_session_dir,
        ),
        **launch_options,
    )


def _resolve_effective_fork_target_harness(
    create_input: SpawnCreateInput,
    *,
    resolved_project_root: Path | None = None,
) -> str:
    preview_input = create_input
    if resolved_project_root is not None:
        preview_input = create_input.model_copy(
            update={"project_root": resolved_project_root.as_posix()},
        )

    validated_payload, preflight_warning = validate_create_input(preview_input)
    preview_request = build_create_payload(
        validated_payload,
        preflight_warning=preflight_warning,
    ).request
    resolved_harness = (preview_request.harness or "").strip()
    if not resolved_harness:
        raise ValueError("Fork target harness could not be resolved.")
    return resolved_harness


def spawn_fork_sync(
    payload: SpawnForkInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnActionOutput:
    project_root, runtime_root = _resolve_spawn_read_authority(
        project_root=payload.project_root,
        prepared=prepared,
    )
    normalized_source_ref = payload.source_ref.strip()
    if not normalized_source_ref:
        raise ValueError("Session reference is required.")

    resolved_reference = resolve_session_reference(
        project_root,
        normalized_source_ref,
        runtime_root=runtime_root,
    )
    if resolved_reference.missing_harness_session_id:
        raise ValueError(_missing_follow_up_session_error(normalized_source_ref))

    requested_model = payload.model.strip()
    requested_agent = payload.agent
    requested_work = payload.work.strip()
    requested_task_dir = (payload.task_dir or "").strip() or None
    requested_goal = payload.goal
    if requested_goal is None and _looks_like_spawn_ref(normalized_source_ref):
        source_row = read_spawn_row(
            project_root,
            normalized_source_ref,
            runtime_root=runtime_root,
        )
        if source_row is not None:
            requested_goal = source_row.goal
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
    target_harness = _resolve_effective_fork_target_harness(
        unresolved_create_input,
        resolved_project_root=project_root,
    )
    if source_harness is not None and source_harness != target_harness:
        raise ValueError(
            "Cannot fork across harnesses: "
            f"source is '{source_harness}', target is '{target_harness}'."
        )

    create_input = unresolved_create_input.model_copy(
        update={"harness": target_harness},
    )
    if prepared is not None:
        return spawn_create_sync(create_input, ctx=ctx, sink=sink, prepared=prepared)
    return spawn_create_sync(create_input, ctx=ctx, sink=sink)


async def spawn_fork(
    payload: SpawnForkInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnActionOutput:
    return await asyncio.to_thread(
        spawn_fork_sync,
        payload,
        ctx=ctx,
        sink=sink,
        prepared=prepared,
    )


def spawn_continue_sync(
    payload: SpawnContinueInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnActionOutput:
    project_root, runtime_root = _resolve_spawn_read_authority(
        project_root=payload.project_root,
        prepared=prepared,
    )
    resolved_spawn_id, source_spawn, resolved_reference = _source_spawn_for_follow_up(
        payload.spawn_id,
        project_root,
        runtime_root=runtime_root,
    )
    if resolved_reference.missing_harness_session_id:
        raise ValueError(
            f"Spawn '{resolved_spawn_id}' has no recorded session — cannot continue/fork."
        )

    _reject_continue_policy_overrides(payload)
    create_input = _build_continue_create_input(
        payload=payload,
        source_spawn=source_spawn,
        source_spawn_id=resolved_spawn_id,
        resolved_reference=resolved_reference,
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
