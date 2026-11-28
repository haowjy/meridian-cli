"""Spawn control: cancel, cancel-all, and the done and rearm signals."""

import asyncio
from pathlib import Path

from meridian.lib.bootstrap.services import (
    RuntimeWriteContext,
    build_spawn_application_service_from_roots,
)
from meridian.lib.context.runtime import RuntimeContext
from meridian.lib.core.domain import (
    ACTIVE_SPAWN_STATUSES,
    TERMINAL_SPAWN_STATUSES,
    SpawnStatus,
    is_terminal_spawn_status,
)
from meridian.lib.core.refs import RefNotFound
from meridian.lib.core.sink import OutputSink
from meridian.lib.core.types import SpawnId
from meridian.lib.ops.locate import (
    resolve_spawn_target,
)
from meridian.lib.ops.runtime import (
    resolve_runtime_root,
    resolve_runtime_root_and_config,
    runtime_context,
)
from meridian.lib.ops.work_membership import active_session_work_ids, spawn_work_id
from meridian.lib.spawn.lifecycle import SpawnLifecycleService
from meridian.lib.spawn.service import CancelOutcome
from meridian.lib.state import spawn_store
from meridian.lib.state.caller import same_runtime_root
from meridian.lib.state.history_index import indexed_spawn_scan
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_signals import (
    RearmRequest,
    SpawnSignalKind,
    read_accepted_signals,
    write_spawn_signal,
)
from meridian.lib.state.spawn_status import (
    read_derived,
)
from meridian.lib.state.spawn_tree import descendant_id_set
from meridian.lib.telemetry.init import setup_telemetry
from meridian.lib.telemetry.observer import register_spawn_telemetry_observer
from meridian.lib.telemetry.observers import register_debug_trace_observer

from .models import (
    SpawnActionOutput,
    SpawnCancelAllInput,
    SpawnCancelAllOutput,
    SpawnCancelInput,
    SpawnRearmInput,
    SpawnSignalInput,
)
from .owed import (
    record_result_shown_to_caller,
)
from .roots import (
    project_root_from_prepared,
    resolve_caller_spawn_id,
    runtime_root_from_prepared_for_read,
)


def _spawn_cancel_output_from_outcome(outcome: CancelOutcome) -> SpawnActionOutput:
    if outcome.already_terminal:
        message = f"Spawn '{outcome.spawn_id}' is already {outcome.status}."
    elif outcome.status == "finalizing":
        message = "Spawn did not terminate within grace; reaper will reconcile."
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


def _resolve_signal_spawn(
    *,
    project_root: Path,
    runtime_root: Path,
    spawn_id: str | None,
    ctx: RuntimeContext | None,
) -> tuple[SpawnRecord, Path]:
    """The spawn to signal and the runtime root it lives in."""

    candidate = resolve_caller_spawn_id(spawn_id, ctx)
    target = resolve_spawn_target(project_root, candidate, runtime_root=runtime_root)
    record = spawn_store.get_spawn(target.runtime_root, target.spawn_id)
    if record is None:
        raise RefNotFound(target.spawn_id, "spawn")
    if record.record_mode == "historical":
        raise ValueError("Historical records have no live process ownership")
    return record, target.runtime_root


_BARE_REARM_WARNING = (
    '`meridian spawn rearm` without `--reason "..."` is deprecated; '
    "the next minor release requires a reason."
)


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
    rearm: RearmRequest | None = None,
    ctx: RuntimeContext | None = None,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnActionOutput:
    _ = sink
    if prepared is not None:
        project_root = project_root_from_prepared(prepared)
        runtime_root = runtime_root_from_prepared_for_read(
            prepared,
            project_root=project_root,
        )
    else:
        project_root, _ = resolve_runtime_root_and_config(payload.project_root)
        runtime_root = resolve_runtime_root(project_root)
    try:
        target, runtime_root = _resolve_signal_spawn(
            project_root=project_root,
            runtime_root=runtime_root,
            spawn_id=payload.spawn_id,
            ctx=ctx,
        )
        resolved_spawn_id = target.id
        launch = LaunchPaths.for_row(runtime_root, target)
        refusal: str | None = None
        reported = False

        def consumer_reads_signal(stored: SpawnRecord) -> bool:
            # Decided under the published-artifact lock that guards the write.
            nonlocal refusal, reported
            row = read_derived(runtime_root, stored.id) or stored
            accepted = read_accepted_signals(launch)
            refusal = _signal_refusal(row, kind, accepted)
            reported = accepted is not None
            return refusal is None

        if not write_spawn_signal(
            launch,
            kind,
            can_write=consumer_reads_signal,
            rearm=rearm,
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
        warning=_BARE_REARM_WARNING if rearm is not None and rearm.reason is None else None,
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
    payload: SpawnRearmInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnActionOutput:
    return _spawn_signal_sync(
        payload,
        kind="rearm",
        command="spawn.rearm",
        rearm=RearmRequest(long=payload.long, reason=payload.reason),
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
        project_root = project_root_from_prepared(prepared)
        runtime_root = runtime_root_from_prepared_for_read(
            prepared,
            project_root=project_root,
        )
    else:
        project_root, _ = resolve_runtime_root_and_config(payload.project_root)
        runtime_root = resolve_runtime_root(project_root)
    # Resolve first: the target's own project decides which hooks and which root the
    # cancel runs under.
    target = resolve_spawn_target(project_root, payload.spawn_id, runtime_root=runtime_root)
    spawn_id = str(target.spawn_id)
    service_project = project_root
    lifecycle: SpawnLifecycleService | None = None
    hooks_skipped = False
    if not same_runtime_root(target.runtime_root, runtime_root):
        runtime_root = target.runtime_root
        if target.project_root is not None and target.project_root.is_dir():
            service_project = target.project_root
        else:
            # The hooks' config went with the directory: there is nothing to load, and a
            # session that can be found by ID must still be stoppable.
            lifecycle = SpawnLifecycleService(runtime_root)
            hooks_skipped = True
    spawn_service = build_spawn_application_service_from_roots(
        service_project, runtime_root, lifecycle=lifecycle
    )
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
            launch_number=outcome.launch_number,
        )
    output = _spawn_cancel_output_from_outcome(outcome)
    if hooks_skipped:
        output = output.model_copy(
            update={
                "message": f"{output.message} Its project directory is gone, so no project "
                "hooks ran."
            }
        )
    return output


def spawn_cancel_all_sync(
    payload: SpawnCancelAllInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnCancelAllOutput:
    resolved_context = runtime_context(ctx)
    if prepared is not None:
        project_root = project_root_from_prepared(prepared)
        runtime_root = runtime_root_from_prepared_for_read(
            prepared,
            project_root=project_root,
        )
    else:
        project_root, _ = resolve_runtime_root_and_config(payload.project_root)
        runtime_root = resolve_runtime_root(project_root)
    work_id = (payload.work or "").strip() or None
    caller_chat_id = (resolved_context.chat_id or "").strip() or None
    caller_spawn_id = str(resolved_context.spawn_id) if resolved_context.spawn_id else None
    # Descendant-scope doesn't need chat_id; only raise when we have neither anchor.
    if caller_chat_id is None and caller_spawn_id is None and not payload.include_others:
        raise ValueError(
            "No session context (MERIDIAN_CHAT_ID not set). "
            "Use --include-others to cancel all non-primary running spawns."
        )

    stored_rows = indexed_spawn_scan(runtime_root).records
    active_work_ids = active_session_work_ids(runtime_root) if work_id is not None else {}

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
        and (work_id is None or spawn_work_id(row, active_work_ids) == work_id)
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


def spawn_cancel_sync(
    payload: SpawnCancelInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> SpawnActionOutput:
    return asyncio.run(_spawn_cancel_impl(payload, ctx=ctx, sink=sink, prepared=prepared))
