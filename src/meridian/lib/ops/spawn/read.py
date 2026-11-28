"""Read-only spawn operations: list, children, stats, show, status, subagents."""

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from meridian.lib.bootstrap.services import (
    RuntimeReadContext,
)
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.refs import RefNotFound
from meridian.lib.core.sink import OutputSink
from meridian.lib.core.spawn_lifecycle import (
    ACTIVE_SPAWN_STATUSES,
    ALL_SPAWN_STATUSES,
    is_active_spawn_status,
)
from meridian.lib.core.spawn_start import resolve_spawn_display_label
from meridian.lib.ops.locate import (
    resolve_spawn_target,
)
from meridian.lib.ops.mars import mars_list_agents
from meridian.lib.ops.runtime import (
    runtime_context,
)
from meridian.lib.ops.spawn.idle import spawn_idle
from meridian.lib.ops.work_attachment import resolve_current_work_alias
from meridian.lib.ops.work_membership import work_spawns
from meridian.lib.platform.locking import try_lock_file
from meridian.lib.state import history_index, session_store
from meridian.lib.state.history_index import indexed_spawn_scan
from meridian.lib.state.machine_identity import machine_identity_or_none
from meridian.lib.state.machine_ids import recent_claim_roots
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.primary_meta import (
    read_primary_surface_metadata,
)
from meridian.lib.state.runtime_root import runtime_root_project_id
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_status import (
    overlay_derived,
)
from meridian.lib.state.spawn_tree import collect_descendants
from meridian.lib.state.user_paths import get_user_home, project_lifetime_lock_path

from .models import (
    ModelStats,
    SpawnChildrenInput,
    SpawnDetailOutput,
    SpawnListEntry,
    SpawnListInput,
    SpawnListOutput,
    SpawnLocation,
    SpawnShowInput,
    SpawnStatsChild,
    SpawnStatsInput,
    SpawnStatsOutput,
    SpawnStatusInput,
    SpawnSubagentEntry,
    SpawnSubagentsInput,
    SpawnSubagentsOutput,
)
from .owed import (
    consume_result_if_shown,
)
from .query import (
    denied_requests,
    detail_from_row,
    read_spawn_row,
)
from .roots import resolve_caller_spawn_id, resolve_spawn_read_authority


def _surface_primary_activity(status: str, activity: str | None) -> str | None:
    """A running primary's activity; ``unknown`` while its harness has reported none."""

    if not is_active_spawn_status(status):
        return None
    return (activity or "").strip() or "unknown"


def _chat_work_ids(runtime_root: Path, rows: Sequence[SpawnRecord]) -> dict[str, str | None]:
    """Each row's chat's current work item, as `spawn show` reports it (the row keeps launch-time).

    One pass over the session store for all rows.
    """

    return {
        record.chat_id: record.active_work_id
        for record in session_store.get_session_records(
            runtime_root, {row.entry_chat_id for row in rows if row.entry_chat_id}
        )
    }


def _spawn_display_label(row: SpawnRecord) -> str | None:
    return resolve_spawn_display_label(row.goal, row.desc, row.display_label)


def _filter_listed(payload: SpawnListInput, spawns: list[SpawnRecord]) -> list[SpawnRecord]:
    """Apply the list's status, model, profile and primary filters."""

    if payload.statuses:
        wanted_statuses = set(payload.statuses)
        spawns = [row for row in spawns if row.status in wanted_statuses]
    elif payload.statuses == ():
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
    return spawns


def _listed_rows(runtime_root: Path, payload: SpawnListInput) -> list[SpawnRecord]:
    """The rows ``payload`` can list, oldest first, from the index.

    Unless the list is of every status, the index is asked for the stored-active rows and
    the wanted statuses only, as ``locate_run`` asks for the active ones: reading a project's
    whole history to show a handful of live rows costs seconds on a busy project.
    ``overlay_derived`` moves a stored-active row to a terminal status and no other way, so
    the stored-active rows hold every row that can be listed as running.
    """

    stored: set[str] | None = None
    if payload.statuses != ():
        wanted = payload.statuses or ((payload.status,) if payload.status is not None else ())
        stored = {*ACTIVE_SPAWN_STATUSES, *map(str, wanted)}
    scan = indexed_spawn_scan(runtime_root, statuses=stored)
    return overlay_derived(runtime_root, scan.records)


def _list_entries(
    runtime_root: Path, rows: Sequence[SpawnRecord], *, machine: str | None
) -> list[SpawnListEntry]:
    chat_work = _chat_work_ids(runtime_root, rows)
    project_id = runtime_root_project_id(runtime_root)
    entries: list[SpawnListEntry] = []
    for row in rows:
        kind = "primary" if (row.kind or "").strip() == "primary" else None
        managed_backend = False
        activity: str | None = None
        surfaced_activity: str | None = None
        session_id = row.harness_session_id
        if kind == "primary":
            metadata = read_primary_surface_metadata(LaunchPaths.for_row(runtime_root, row))
            managed_backend = metadata.managed_backend
            activity = metadata.activity
            session_id = metadata.harness_session_id or session_id
            surfaced_activity = _surface_primary_activity(row.status, activity)
        entries.append(
            SpawnListEntry(
                spawn_id=row.id,
                status=row.status,
                status_display=(
                    f"{row.status} ({surfaced_activity})"
                    if surfaced_activity not in {None, "unknown"}
                    else None
                ),
                model=row.model or "",
                agent=row.agent or None,
                work_id=chat_work.get(row.entry_chat_id or ""),
                idle=spawn_idle(row, session_id, activity),
                kind=kind,
                activity=surfaced_activity,
                managed_backend=managed_backend,
                duration_secs=row.total_usage.duration_secs,
                cost_usd=row.total_usage.total_cost_usd,
                located=SpawnLocation(
                    project_root=row.control_root,
                    project_id=project_id,
                    machine=machine,
                ),
            )
        )
    return entries


def _machine_name() -> str | None:
    identity = machine_identity_or_none()
    return identity.name if identity is not None else None


_NEW_CLAIM_WINDOW_SECONDS = 120.0


def _live_project_roots() -> list[Path]:
    """Runtime roots with a running session: the ones whose lifetime lock is held.

    A session holds its project's lifetime lock shared for as long as it runs, so a lock
    that can be taken exclusively, or that does not exist, means no live session. The
    lock is only tried and released at once; no project's rows are opened to find out.
    """

    projects = get_user_home() / "projects"
    try:
        roots = sorted(path for path in projects.iterdir() if path.is_dir())
    except FileNotFoundError:
        return []
    live: list[Path] = []
    for root in roots:
        lock_path = project_lifetime_lock_path(root)
        if not lock_path.exists():
            continue
        with try_lock_file(lock_path, reentrant=False, read_only_existing=True) as handle:
            if handle is None:
                live.append(root)
    return live


def _spawn_list_all_projects(payload: SpawnListInput) -> SpawnListOutput:
    """Running sessions in every project on this machine."""

    if payload.statuses == ():
        raise ValueError(
            "--global lists running sessions only: --all is a scan of every project's "
            "history. Use `meridian session search --global` to search it."
        )
    wanted = payload.statuses or ((payload.status,) if payload.status is not None else ())
    if payload.failed or payload.work or not all(is_active_spawn_status(item) for item in wanted):
        raise ValueError(
            "--global lists running sessions only; it takes no --status, --view, --failed or "
            "--work for finished or per-project history. "
            "Use `meridian session search --global` to search it."
        )
    machine = _machine_name()
    found: list[tuple[Path, SpawnRecord]] = []
    # A spawn reports running before its runner takes the lifetime lock, so the roots of
    # claims from the last two minutes are opened too.
    # The lock scan and a claim can spell one root two ways (``/tmp`` and ``/private/tmp``).
    roots = {
        root.resolve(): root
        for root in (*_live_project_roots(), *recent_claim_roots(_NEW_CLAIM_WINDOW_SECONDS))
    }
    for root in sorted(roots.values()):
        if not root.is_dir():  # a claim's root that was pruned
            continue
        try:
            rows = _listed_rows(root, payload)
        except (OSError, history_index.HistoryIndexBusy, sqlite3.Error):
            continue  # pruned or rebuilt while the list was being built: it reads as empty
        found.extend((root, row) for row in _filter_listed(payload, rows))
    found.sort(key=lambda item: item[1].started_at or "", reverse=True)
    selected = found[: payload.limit]
    entries_by_root = {
        root: {
            entry.spawn_id: entry
            for entry in _list_entries(
                root, [row for owner, row in selected if owner == root], machine=machine
            )
        }
        for root in dict.fromkeys(root for root, _ in selected)
    }
    entries = [entries_by_root[root][row.id] for root, row in selected]
    return SpawnListOutput(
        spawns=tuple(entries),
        total_count=len(found) if len(found) > len(selected) else None,
        truncated=len(found) > len(selected),
        show_project=True,
    )


def spawn_list_sync(
    payload: SpawnListInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnListOutput:
    _ = (ctx, sink)
    if payload.all_projects:
        return _spawn_list_all_projects(payload)
    if prepared is not None and prepared.runtime_root is None:
        return SpawnListOutput(spawns=())
    project_root, runtime_root = resolve_spawn_read_authority(
        project_root=payload.project_root,
        prepared=prepared,
    )
    if runtime_root is None:
        return SpawnListOutput(spawns=())

    work_id = resolve_current_work_alias(
        payload.work,
        usage="meridian spawn list --work <work-id>",
        project_root=project_root,
        runtime_root=runtime_root,
    )
    if work_id is not None:
        spawns = list(reversed(work_spawns(runtime_root, work_id)))
    else:
        spawns = list(reversed(_listed_rows(runtime_root, payload)))
    spawns = _filter_listed(payload, spawns)

    # When statuses is empty tuple, show all statuses but cap intelligently:
    # always include all active spawns, pad with recent non-active up to limit.
    show_all = payload.statuses == ()
    total_count = len(spawns)
    limit = payload.limit

    if show_all:
        # Always include all active spawns, fill remaining slots with recent non-active.
        active = [row for row in spawns if is_active_spawn_status(row.status)]
        non_active = [row for row in spawns if not is_active_spawn_status(row.status)]
        remaining = None if limit is None else max(len(active), limit) - len(active)
        selected = active + non_active[:remaining]
    else:
        selected = spawns[:limit]

    truncated = total_count > len(selected)
    return SpawnListOutput(
        spawns=tuple(_list_entries(runtime_root, selected, machine=_machine_name())),
        total_count=total_count if truncated else None,
        truncated=truncated,
    )


def spawn_children_sync(
    payload: SpawnChildrenInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnListOutput:
    _ = sink
    project_root, runtime_root = resolve_spawn_read_authority(
        project_root=payload.project_root,
        prepared=prepared,
    )
    caller_ref = resolve_caller_spawn_id(payload.spawn_id, ctx)
    try:
        target = resolve_spawn_target(project_root, caller_ref, runtime_root=runtime_root)
    except RefNotFound:
        if runtime_root is not None:
            raise
        return SpawnListOutput(spawns=(), text_view="children")
    runtime_root = target.runtime_root

    children = list(
        reversed(
            overlay_derived(
                runtime_root,
                indexed_spawn_scan(
                    runtime_root,
                    parent_id=target.spawn_id,
                ).records,
            )
        )
    )
    chat_work = _chat_work_ids(runtime_root, children)
    entries = tuple(
        SpawnListEntry(
            spawn_id=row.id,
            status=row.status,
            model=row.model or "",
            agent=row.agent or None,
            work_id=chat_work.get(row.entry_chat_id or ""),
            idle=spawn_idle(row, row.harness_session_id, None),
            desc=_spawn_display_label(row),
            duration_secs=row.total_usage.duration_secs,
            cost_usd=row.total_usage.total_cost_usd,
        )
        for row in children
    )
    return SpawnListOutput(spawns=entries, text_view="children")


def spawn_stats_sync(
    payload: SpawnStatsInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnStatsOutput:
    _ = (ctx, sink)
    _project_root, runtime_root = resolve_spawn_read_authority(
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
        cost = row.total_cost_usd
        if cost is not None:
            acc["cost_usd"] = float(acc["cost_usd"]) + cost
            total_cost_usd += cost
        duration = row.total_duration_secs
        if duration is not None:
            total_duration_secs += duration

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
                duration_secs=s.total_usage.duration_secs,
                cost_usd=s.total_usage.total_cost_usd,
                input_tokens=s.total_usage.input_tokens,
                output_tokens=s.total_usage.output_tokens,
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


def spawn_show_sync(
    payload: SpawnShowInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnDetailOutput:
    _ = sink
    resolved_context = runtime_context(ctx)
    project_root, runtime_root = resolve_spawn_read_authority(
        project_root=payload.project_root,
        prepared=prepared,
    )
    target = resolve_spawn_target(project_root, payload.spawn_id, runtime_root=runtime_root)
    spawn_id = target.spawn_id
    runtime_root = target.runtime_root
    row = read_spawn_row(project_root, spawn_id, runtime_root=runtime_root)
    if row is None:
        raise RefNotFound(spawn_id, "spawn")
    kind = "primary" if (row.kind or "").strip() == "primary" else None
    managed_backend = False
    activity: str | None = None
    backend_pid: int | None = None
    tui_pid: int | None = None
    backend_port: int | None = None
    harness_session_id: str | None = None
    if kind == "primary":
        metadata = read_primary_surface_metadata(LaunchPaths.for_row(runtime_root, row))
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
    surfaced_activity = (
        _surface_primary_activity(row.status, activity) if kind == "primary" else None
    )
    # The chat's current item, which `work start`/`switch` may have changed since launch.
    chat_id = row.entry_chat_id
    current_work = (
        session_store.get_session_active_work_id(runtime_root, chat_id) if chat_id else None
    )
    result = detail.model_copy(
        update={
            "work_id": current_work,
            "denied_requests": denied_requests(LaunchPaths.for_row(runtime_root, row)),
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
        launch_number=row.launch_number,
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


def spawn_subagents_sync(
    payload: SpawnSubagentsInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
    prepared: RuntimeReadContext | None = None,
) -> SpawnSubagentsOutput:
    _ = sink
    project_root, runtime_root = resolve_spawn_read_authority(
        project_root=payload.project_root,
        prepared=prepared,
    )
    spawn_id = str(runtime_context(ctx).spawn_id or "").strip() or None
    row = (
        read_spawn_row(project_root, spawn_id, runtime_root=runtime_root)
        if spawn_id is not None and runtime_root is not None
        else None
    )
    agent = ((row.agent or "").strip() or None) if row is not None else None
    harness = (row.harness or "").strip() if row is not None else ""

    catalog = mars_list_agents(project_root)
    caller = next((entry for entry in catalog if entry.name == agent), None)
    # The caller's `subagents:` list is advisory: the default view, with `--all` showing
    # what lies outside it.
    declared = frozenset(caller.subagents) if caller is not None else frozenset[str]()
    agents = tuple(
        SpawnSubagentEntry(
            name=entry.name,
            scope=entry.scope,
            description=entry.description,
            mode=entry.mode,
            model_invocable=entry.model_invocable,
            # A fanout agent is native too, but Meridian stays its route.
            native=harness in entry.native_harnesses and not entry.fanout,
            declared=agent is None or entry.name in declared,
        )
        for entry in catalog
        if payload.all or (entry.name in declared and entry.model_invocable)
    )
    empty: Literal["no_current_agent", "no_declared_agents"] | None = None
    if not agents and not payload.all:
        empty = "no_current_agent" if agent is None else "no_declared_agents"
    return SpawnSubagentsOutput(agents=agents, empty=empty)
