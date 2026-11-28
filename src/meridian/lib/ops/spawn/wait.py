"""`spawn wait`: block until named or owed spawns finish, with checkpoints."""

import os
import sqlite3
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from meridian.lib.bootstrap.services import (
    RuntimeReadContext,
)
from meridian.lib.config.models import MeridianConfig
from meridian.lib.context.runtime import RuntimeContext
from meridian.lib.core.domain import (
    ACTIVE_SPAWN_STATUSES,
    FAILURE_SPAWN_STATUSES,
    is_active_spawn_status,
    is_terminal_spawn_status,
)
from meridian.lib.core.sink import NullSink, OutputSink
from meridian.lib.ops.locate import (
    resolve_spawn_targets,
)
from meridian.lib.ops.runtime import (
    resolve_runtime_root_and_config_for_read,
    resolve_runtime_root_for_read,
    runtime_context,
)
from meridian.lib.state import history_index, spawn_store
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_observations import LaunchKey, WaitPin, live_wait_pins
from meridian.lib.state.spawn_observations import record as record_spawn_observation
from meridian.lib.state.spawn_ownership import owned_parent_ids
from meridian.lib.state.spawn_signals import (
    read_hold_status,
)
from meridian.lib.state.spawn_status import (
    LaunchNotFound,
    Running,
    overlay_derived,
    poll_launch,
    read_derived,
)
from meridian.lib.state.spawn_tree import collect_descendants

from .models import (
    CheckpointHold,
    SpawnDetailOutput,
    SpawnListEntry,
    SpawnWaitInput,
    SpawnWaitMultiOutput,
    format_spawn_identity_table,
)
from .owed import (
    delivery_details,
    owed_consumed_ids,
    resolve_owed_finished_rows,
    select_delivery_rows,
)
from .query import (
    detail_from_row,
    read_spawn_row,
)
from .roots import config_from_prepared, project_root_from_prepared

_WAIT_PROGRESS_INTERVAL_SECS = 5.0


_WAIT_POLL_INTERVAL_SECS = 0.25


@dataclass(frozen=True)
class _WaitTargetDiscovery:
    blocked_on_ids: tuple[str, ...] = ()
    active_rows: tuple[SpawnRecord, ...] = ()
    owed_rows: tuple[SpawnRecord, ...] = ()
    # Every finished owed launch seen, including rows the delivery cap held back.
    owed_candidate_refs: frozenset[LaunchKey] = frozenset()
    discovery_pending: bool = False


def _build_wait_timeout_message(pending_spawn_ids: set[str], elapsed_secs: float) -> str:
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


def _spawn_is_terminal(status: str) -> bool:
    return is_terminal_spawn_status(status)


def _resolve_wait_targets(
    payload: SpawnWaitInput,
    runtime_root: Path | None,
    ctx: RuntimeContext,
    *,
    deadline: float,
    excluded_owed_refs: frozenset[LaunchKey] = frozenset(),
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
    if not chat_id or runtime_root is None:
        raise ValueError(
            "No-arg wait requires MERIDIAN_CHAT_ID (run from inside a meridian session)"
        )

    if not ctx.spawn_id:
        raise ValueError("No-arg wait requires MERIDIAN_SPAWN_ID")
    caller_spawn_id = str(ctx.spawn_id)
    try:
        roots = owned_parent_ids(
            runtime_root,
            read_derived(runtime_root, caller_spawn_id),
            chat_id=chat_id,
            caller_spawn_id=caller_spawn_id,
            deadline=deadline,
        )
        pending = _discover_pending_spawns(runtime_root, roots, deadline=deadline)
        owed_candidates = resolve_owed_finished_rows(
            runtime_root=runtime_root,
            chat_id=chat_id,
            caller_spawn_id=caller_spawn_id,
            parent_ids=roots,
            excluded_refs=excluded_owed_refs.union(LaunchKey.of(row) for row in pending),
            deadline=deadline,
        )
    except history_index.HistoryIndexBusy:
        return _WaitTargetDiscovery(discovery_pending=True)

    return _WaitTargetDiscovery(
        blocked_on_ids=tuple(row.id for row in pending),
        active_rows=tuple(pending),
        owed_rows=select_delivery_rows(owed_candidates),
        owed_candidate_refs=frozenset(LaunchKey.of(row) for row in owed_candidates),
    )


def _discover_pending_spawns(
    runtime_root: Path,
    roots: tuple[str, ...],
    *,
    deadline: float | None = None,
) -> list[SpawnRecord]:
    """Discover active spawns in the transitive subtrees of ``roots``.

    Roots are ``owned_parent_ids``, the same set owed results use: a live primary
    walks every primary generation of its chat, so background work started before a
    continue is found; anyone else walks its own subtree. Peers that merely share the
    chat are not under any root. The index selects membership; lifecycle decisions
    re-read authority. Recursive traversal retains terminal and archived ancestry so
    it cannot lose grandchildren.
    """
    selected = history_index.HistoryIndex(runtime_root).descendant_projection(
        roots,
        active_only=True,
        deadline=deadline,
    )

    candidates: list[SpawnRecord] = []
    for spawn_id, _parent, _archived in selected:
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
            excluded_owed_refs=initial_targets.owed_candidate_refs.union(
                LaunchKey.of(row) for row in initial_targets.active_rows
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
    runtime_roots: Mapping[str, Path],
    sink: OutputSink,
) -> None:
    entries: list[SpawnListEntry] = []
    for spawn_id in spawn_ids:
        row = read_spawn_row(project_root, spawn_id, runtime_root=runtime_roots[spawn_id])
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


def _checkpoint_hold(runtime_root: Path, row: SpawnRecord) -> CheckpointHold | None:
    status = read_hold_status(LaunchPaths.for_row(runtime_root, row))
    if status is None:
        return None
    idle_secs: float | None = None
    if status.last_work_at is not None:
        with suppress(ValueError):
            last_work = datetime.fromisoformat(status.last_work_at)
            idle_secs = max(0.0, (datetime.now(UTC) - last_work).total_seconds())
    return CheckpointHold(
        blockers=status.blockers, last_work_at=status.last_work_at, idle_secs=idle_secs
    )


def _build_wait_checkpoint_output(
    details: tuple[SpawnDetailOutput, ...],
    *,
    pending_ids: tuple[str, ...],
    runtime_roots: Mapping[str, Path],
    chat_id: str | None,
    elapsed_secs: float,
) -> SpawnWaitMultiOutput:
    checkpoint_descendants: dict[str, tuple[str, ...]] = {}
    checkpoint_holds: dict[str, CheckpointHold | None] = {}
    for runtime_root in dict.fromkeys(runtime_roots[spawn_id] for spawn_id in pending_ids):
        tree_rows = overlay_derived(
            runtime_root,
            spawn_store.list_spawns(runtime_root).records,
        )
        rows_by_id = {row.id: row for row in tree_rows}
        for spawn_id in (i for i in pending_ids if runtime_roots[i] == runtime_root):
            checkpoint_descendants[spawn_id] = tuple(
                row.id
                for row in collect_descendants(spawn_id, tree_rows)
                if row.id != spawn_id and is_active_spawn_status(row.status)
            )
            if spawn_id in rows_by_id:
                checkpoint_holds[spawn_id] = _checkpoint_hold(runtime_root, rows_by_id[spawn_id])
    return _build_wait_multi_output(details).model_copy(
        update={
            "checkpoint": True,
            "checkpoint_pending_ids": pending_ids,
            "checkpoint_holds": {
                spawn_id: hold for spawn_id, hold in checkpoint_holds.items() if hold is not None
            },
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
        project_root = project_root_from_prepared(prepared)
        config = config_from_prepared(prepared)
        runtime_root = prepared.runtime_root or resolve_runtime_root_for_read(project_root)
    else:
        project_root, config = resolve_runtime_root_and_config_for_read(payload.project_root)
        runtime_root = resolve_runtime_root_for_read(project_root)
    has_explicit_ids = bool(payload.spawn_ids) or bool(
        payload.spawn_id is not None and payload.spawn_id.strip()
    )
    # A claimed ID names a session in any project, so a caller whose own project has no
    # state can still wait on one.
    if runtime_root is None and not has_explicit_ids:
        active_sink.status("No pending spawns.")
        return SpawnWaitMultiOutput()
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
    runtime_roots: dict[str, Path] = {}
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
        located = resolve_spawn_targets(project_root, blocked_on_ids, runtime_root=runtime_root)
        blocked_on_ids = tuple(str(target.spawn_id) for target in located)
        runtime_roots = {str(target.spawn_id): target.runtime_root for target in located}
    elif runtime_root is not None:
        runtime_roots = dict.fromkeys(blocked_on_ids, runtime_root)

    # A target in another project is watched, not owned: nothing is recorded for the
    # caller there, because the caller's ID means nothing in that root.
    own_ids = frozenset(
        spawn_id
        for spawn_id, root in runtime_roots.items()
        if resolved_context.caller_in(root) is not None
    )
    parent_wait_observer_id = (
        str(resolved_context.spawn_id)
        if payload.observe and resolved_context.caller_in(runtime_root)
        else None
    )
    # Each target is waited on as the launch it is now, or the one an earlier run of this
    # wait pinned (a `--yield-after-secs` re-run is a new process). Pins are stored only for
    # an explicit wait by a caller with a spawn ID; any other wait pins in this process.
    stored_pins = (
        live_wait_pins(
            runtime_root,
            parent_wait_observer_id,
            owed_consumed_ids(runtime_root, (parent_wait_observer_id,)),
        )
        if runtime_root is not None and parent_wait_observer_id is not None and has_explicit_ids
        else {}
    )
    pins: dict[str, int] = {}
    for spawn_id in blocked_on_ids:
        record = spawn_store.get_spawn(runtime_roots[spawn_id], spawn_id)
        if record is not None and record.record_mode == "historical":
            raise ValueError("Historical records cannot be waited on")
        pins[spawn_id] = stored_pins.get(spawn_id) or (record.launch_number if record else 1)
    for owed_row in owed_rows:
        if owed_row.record_mode == "historical":
            raise ValueError("Historical records cannot be waited on")
    observed_keys = tuple(
        (
            *(str(LaunchKey(spawn_id, n)) for spawn_id, n in pins.items() if spawn_id in own_ids),
            *(str(LaunchKey.of(row)) for row in owed_rows),
        )
    )

    poll = (
        payload.poll_interval_secs
        if payload.poll_interval_secs is not None
        else _WAIT_POLL_INTERVAL_SECS
    )
    if poll <= 0:
        poll = _WAIT_POLL_INTERVAL_SECS

    lease_seconds = max(30.0, poll * 2 + 5)

    def pin_documents() -> dict[str, WaitPin]:
        if not has_explicit_ids:
            return {}
        expires_at = time.time() + checkpoint_seconds
        return {
            target: WaitPin(n=n, expires_at=expires_at)
            for target, n in pins.items()
            if target in own_ids
        }

    def observe(**changes: Any) -> None:
        if runtime_root is not None:
            record_spawn_observation(
                runtime_root=runtime_root, parent_spawn_id=parent_wait_observer_id, **changes
            )

    observe(waiting_add=observed_keys, pins_set=pin_documents(), lease_seconds=lease_seconds)
    details: tuple[SpawnDetailOutput, ...] = ()
    owed_spawns: tuple[SpawnDetailOutput, ...] = ()
    try:
        if runtime_root is not None:
            owed_spawns = delivery_details(
                project_root=project_root, runtime_root=runtime_root, rows=owed_rows
            )
        if not blocked_on_ids:
            output = _build_wait_multi_output(())
        else:
            _emit_wait_set(
                blocked_on_ids,
                project_root,
                chat_id=wait_chat_id,
                runtime_roots=runtime_roots,
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
                observe(
                    waiting_add=tuple(
                        str(LaunchKey(spawn_id, pins[spawn_id]))
                        for spawn_id in pending
                        if spawn_id in own_ids
                    ),
                    pins_set=pin_documents(),
                    lease_seconds=lease_seconds,
                )
                for spawn_id in tuple(pending):
                    try:
                        launch_poll = poll_launch(runtime_roots[spawn_id], spawn_id, pins[spawn_id])
                    except LaunchNotFound:
                        if has_explicit_ids:
                            raise
                        # No-arg discovery is chat-scoped: if a discovered spawn vanishes while
                        # waiting, treat it as resolved instead of failing unrelated waits.
                        pending.discard(spawn_id)
                        rows.pop(spawn_id, None)
                        continue
                    rows[spawn_id] = launch_poll.row
                    if not isinstance(launch_poll, Running):
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
                        runtime_root=runtime_roots[spawn_id],
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
                        runtime_roots=runtime_roots,
                        chat_id=wait_chat_id,
                        elapsed_secs=now - started,
                    )
                break
        output = output.model_copy(update={"owed_spawns": owed_spawns})
        if not has_explicit_ids and runtime_root is not None:
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
        observe(
            waiting_remove=observed_keys,
            observed_add=(
                *(
                    str(LaunchKey(detail.spawn_id, pins[detail.spawn_id]))
                    for detail in details
                    if _spawn_is_terminal(detail.status) and detail.spawn_id in own_ids
                ),
                *(str(LaunchKey.of(row)) for row in owed_rows),
            ),
        )
