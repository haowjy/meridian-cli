"""Work item dashboard and projection operations."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.core.spawn_start import resolve_spawn_display_label
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.runtime import async_from_sync, resolve_roots_for_read, runtime_context
from meridian.lib.ops.work_sessions import work_session_chat_ids
from meridian.lib.state import session_store, spawn_store, work_store
from meridian.lib.state.spawn.model import SpawnRecord


def _display_path(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def _spawn_id_sort_key(spawn_id: str) -> tuple[int, str]:
    if len(spawn_id) >= 2 and spawn_id[0] in {"p", "r"} and spawn_id[1:].isdigit():
        return (int(spawn_id[1:]), spawn_id)
    return (10**9, spawn_id)


def _spawn_label(spawn: SpawnRecord) -> str:
    """Prefer goal over desc for display — goal is the completion contract."""
    label = resolve_spawn_display_label(spawn.goal, spawn.desc, spawn.display_label)
    if label:
        return " ".join(label.split())
    if spawn.kind == "primary":
        return "(primary)"
    return ""


def _truncated_goal(goal: str | None, *, max_len: int = 80) -> str:
    if goal is None:
        return ""
    normalized = " ".join(goal.split())
    if len(normalized) <= max_len:
        return normalized
    if max_len <= 1:
        return "…"
    return normalized[: max_len - 1].rstrip() + "…"


class WorkDashboardSpawn(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    model: str
    status: str
    desc: str = ""


def _dashboard_spawn(spawn: SpawnRecord) -> WorkDashboardSpawn:
    return WorkDashboardSpawn(
        id=spawn.id,
        model=(spawn.model or "").strip() or "-",
        status=spawn.status,
        desc=_spawn_label(spawn),
    )


def _format_spawn_rows(spawns: tuple[WorkDashboardSpawn, ...], *, indent: str) -> list[str]:
    if not spawns:
        return [f"{indent}(no spawns)"]

    from meridian.lib.core.formatting import tabular

    table = tabular([[spawn.id, spawn.model, spawn.status, spawn.desc] for spawn in spawns])
    return [f"{indent}{line}" for line in table.splitlines()]


class WorkSessionItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    chat_id: str
    harness: str
    harness_session_id: str
    status: str
    model: str
    agent: str


def _format_session_rows(sessions: tuple[WorkSessionItem, ...], *, indent: str) -> list[str]:
    if not sessions:
        return [f"{indent}(no sessions)"]

    from meridian.lib.core.formatting import tabular

    rows = [["chat_id", "harness", "status", "model", "harness_session_id"]]
    rows.extend(
        [
            [
                session.chat_id,
                session.harness,
                session.status,
                session.model,
                session.harness_session_id,
            ]
            for session in sessions
        ]
    )
    return [f"{indent}{line}" for line in tabular(rows).splitlines()]


def _active_session_work_ids(runtime_root: Path) -> dict[str, str]:
    attached: dict[str, str] = {}
    for record in session_store.list_active_session_records(runtime_root):
        active_work_id = record.active_work_id
        if active_work_id:
            attached[record.chat_id] = active_work_id
    return attached


def _effective_work_id(
    spawn: SpawnRecord,
    *,
    active_session_work_ids: dict[str, str],
) -> str | None:
    if spawn.kind == "primary":
        chat_id = (spawn.chat_id or "").strip()
        if not chat_id:
            return None
        return active_session_work_ids.get(chat_id)
    normalized = (spawn.work_id or "").strip()
    return normalized or None


def _associated_with_work_item(
    spawn: SpawnRecord,
    *,
    work_id: str,
    active_session_work_ids: dict[str, str],
) -> bool:
    if spawn.kind == "primary":
        chat_id = (spawn.chat_id or "").strip()
        return (
            bool(chat_id)
            and is_active_spawn_status(spawn.status)
            and active_session_work_ids.get(chat_id) == work_id
        )
    return (spawn.work_id or "").strip() == work_id


def work_dir_display(project_root: Path, project_state_dir: Path, work_id: str) -> str:
    return _display_path(project_root, work_store.work_scratch_dir(project_state_dir, work_id))


def _display_worktree_path(worktree_path: str, project_root: Path) -> str:
    """Display-friendly worktree path, relative to project root parent when possible."""
    try:
        wt = Path(worktree_path)
        rel = wt.relative_to(project_root.parent)
        return f"../{rel.as_posix()}"
    except ValueError:
        return worktree_path


class WorkDashboardItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    status: str
    goal: str | None = None
    task_dir: str | None = None
    spawns: tuple[WorkDashboardSpawn, ...] = ()
    worktree_display: str | None = None  # display-friendly path, pre-computed; None = no worktree
    worktree_exists: bool | None = None  # None = no worktree; True/False = exists/missing
    worktree_pending: bool = False  # creation interrupted


class WorkDashboardInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_root: str | None = None


class WorkDashboardOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[WorkDashboardItem, ...] = ()
    ungrouped_spawns: tuple[WorkDashboardSpawn, ...] = ()
    warnings: tuple[str, ...] = ()

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        lines = ["ACTIVE ACTIVITY"]
        if not self.items and not self.ungrouped_spawns:
            lines.append("  (no active spawns)")
            if self.warnings:
                lines.append("")
                for warning in self.warnings:
                    lines.append(f"warning: {warning}")
            return "\n".join(lines)

        has_spawns = False

        for item in self.items:
            row = f"  {item.name}  {item.status}"
            goal = _truncated_goal(item.goal)
            if goal:
                row = f"{row}  {goal}"
            lines.append(row)
            if item.task_dir is not None:
                lines.append(f"    Task dir: {item.task_dir}")
            lines.extend(_format_spawn_rows(item.spawns, indent="    "))
            if item.spawns:
                has_spawns = True
            lines.append("")

        if self.ungrouped_spawns:
            lines.append("  (no work)")
            lines.extend(_format_spawn_rows(self.ungrouped_spawns, indent="    "))
            has_spawns = True
            lines.append("")

        if lines[-1] == "":
            lines.pop()
        if has_spawns:
            lines.append("")
            lines.append("Run `meridian spawn show <id>` for details.")
        if self.warnings:
            lines.append("")
            for warning in self.warnings:
                lines.append(f"warning: {warning}")
        return "\n".join(lines)


class WorkListItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    status: str
    goal: str | None = None
    description: str
    created_at: str


class WorkListInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    done_only: bool = False
    limit: int = 10
    all_archived: bool = False
    project_root: str | None = None


class WorkListOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[WorkListItem, ...] = ()
    warnings: tuple[str, ...] = ()

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        lines: list[str] = []
        if not self.items:
            lines.append("(no work items)")
        else:
            from meridian.lib.core.formatting import tabular

            rows = [["name", "status", "created"]]
            rows.extend([[item.name, item.status, item.created_at] for item in self.items])
            lines.append(tabular(rows))
        if self.warnings:
            lines.append("")
            for warning in self.warnings:
                lines.append(f"warning: {warning}")
        return "\n".join(lines)


class WorkShowInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_id: str
    project_root: str | None = None


class WorkShowOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    status: str
    goal: str | None = None
    description: str
    created_at: str
    work_dir: str
    task_dir: str | None = None
    spawns: tuple[WorkDashboardSpawn, ...] = ()
    sessions: tuple[WorkSessionItem, ...] = ()
    worktree_path: str | None = None  # full absolute path; None = no worktree
    worktree_exists: bool | None = None  # None = no worktree; True/False = exists/missing
    worktree_pending: bool = False  # creation interrupted

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx

        from meridian.lib.core.formatting import kv_block

        kv_rows: list[tuple[str, str | None]] = [
            ("Work", self.name),
            ("Status", self.status),
            ("Goal", self.goal or "(none)"),
            ("Description", self.description or "(none)"),
            ("Created", self.created_at),
            ("Dir", self.work_dir),
        ]
        kv_rows.append(("Task dir", self.task_dir or "(project root)"))

        lines = [kv_block(kv_rows)]
        if self.spawns:
            lines.append("")
            lines.append("Spawns:")
            lines.extend(_format_spawn_rows(self.spawns, indent="  "))
        lines.append("")
        lines.append("Sessions:")
        lines.extend(_format_session_rows(self.sessions, indent="  "))
        return "\n".join(lines)


class WorkSessionsInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_id: str = ""
    all: bool = False
    primary: bool = False
    project_root: str | None = None


class WorkSessionsOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_id: str
    sessions: tuple[WorkSessionItem, ...] = ()
    all: bool = False
    primary: bool = False

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        lines = _format_session_rows(self.sessions, indent="")
        if not self.all and not self.primary:
            lines.append("")
            lines.append("Use --all to include historical sessions.")
            lines.append("Use --primary to show only the primary handoff chain.")
        return "\n".join(lines)


def _resolve_work_id(
    *,
    payload_work_id: str,
    runtime_root: Path,
    ctx: RuntimeContext | None,
) -> str:
    normalized = payload_work_id.strip()
    if normalized:
        return normalized

    resolved_ctx = runtime_context(ctx)
    if resolved_ctx.work_id is not None and resolved_ctx.work_id.strip():
        return resolved_ctx.work_id.strip()

    chat_id = resolved_ctx.chat_id.strip()
    if chat_id:
        attached_work_id = session_store.get_session_active_work_id(runtime_root, chat_id)
        if attached_work_id is not None and attached_work_id.strip():
            return attached_work_id.strip()

    raise ValueError(
        "No work item resolved. Pass `meridian work sessions <work_id>`, "
        "Use `--work-id` or run from a session attached to a work item."
    )


def _work_sessions_for_work_id(
    project_root: Path,
    runtime_root: Path,
    work_id: str,
    *,
    include_all: bool,
    primary_only: bool = False,
) -> tuple[WorkSessionItem, ...]:
    active_chat_ids = set(session_store.list_active_sessions(runtime_root))
    chat_ids = work_session_chat_ids(
        project_root,
        runtime_root,
        work_id,
        include_all=include_all,
    )
    records = session_store.get_session_records(runtime_root, chat_ids)
    if primary_only:
        records = [r for r in records if r.kind == "primary"]
    records.sort(key=lambda record: (record.started_at, record.chat_id))
    return tuple(
        WorkSessionItem(
            chat_id=record.chat_id,
            harness=(record.harness or "").strip() or "-",
            harness_session_id=(record.harness_session_id or "").strip() or "-",
            status="active" if record.chat_id in active_chat_ids else "stopped",
            model=(record.model or "").strip() or "-",
            agent=(record.agent or "").strip() or "-",
        )
        for record in records
    )


def work_dashboard_sync(
    payload: WorkDashboardInput,
    ctx: RuntimeContext | None = None,
) -> WorkDashboardOutput:
    _ = ctx
    roots = resolve_roots_for_read(payload.project_root)
    if roots is None:
        return WorkDashboardOutput()
    project_root = roots.project_root
    project_state_dir = roots.project_state_dir
    runtime_state_root = roots.runtime_root
    work_items, work_warnings = work_store.list_work_items(project_state_dir)
    items_by_name = {item.name: item for item in work_items}
    active_session_work_ids = _active_session_work_ids(runtime_state_root)
    grouped: dict[str, list[WorkDashboardSpawn]] = {}
    ungrouped: list[WorkDashboardSpawn] = []

    from meridian.lib.state.reaper import reconcile_spawns

    for spawn in reconcile_spawns(
        project_root,
        runtime_state_root,
        spawn_store.list_spawns(runtime_state_root),
    ).records:
        if not is_active_spawn_status(spawn.status):
            continue
        row = _dashboard_spawn(spawn)
        work_id = _effective_work_id(spawn, active_session_work_ids=active_session_work_ids)
        if work_id:
            grouped.setdefault(work_id, []).append(row)
        else:
            ungrouped.append(row)

    items: list[WorkDashboardItem] = []
    for work_id in sorted(
        grouped,
        key=lambda name: (
            items_by_name[name].created_at if name in items_by_name else "9999",
            name,
        ),
    ):
        item = items_by_name.get(work_id)
        wt_path = item.worktree_path if item is not None else None
        wt_pending = item.worktree_pending if item is not None else False
        wt_display: str | None = None
        wt_exists: bool | None = None
        if wt_path is not None:
            wt_display = _display_worktree_path(wt_path, project_root)
            wt_exists = Path(wt_path).is_dir()
        items.append(
            WorkDashboardItem(
                name=work_id,
                status=item.status if item is not None else "missing",
                goal=item.goal if item is not None else None,
                task_dir=item.task_dir if item is not None else None,
                spawns=tuple(
                    sorted(grouped[work_id], key=lambda spawn: _spawn_id_sort_key(spawn.id))
                ),
                worktree_display=wt_display,
                worktree_exists=wt_exists,
                worktree_pending=wt_pending,
            )
        )

    return WorkDashboardOutput(
        items=tuple(items),
        ungrouped_spawns=tuple(sorted(ungrouped, key=lambda spawn: _spawn_id_sort_key(spawn.id))),
        warnings=tuple(work_warnings),
    )


def work_list_sync(
    payload: WorkListInput,
    ctx: RuntimeContext | None = None,
) -> WorkListOutput:
    _ = ctx
    roots = resolve_roots_for_read(payload.project_root)
    if roots is None:
        return WorkListOutput()
    project_state_dir = roots.project_state_dir
    if payload.done_only:
        items, warnings = work_store.list_archived_work_items(
            project_state_dir,
            limit=payload.limit,
            all_archived=payload.all_archived,
        )
    else:
        items, warnings = work_store.list_work_items(project_state_dir)
    return WorkListOutput(
        items=tuple(
            WorkListItem(
                name=item.name,
                status=item.status,
                goal=item.goal,
                description=item.description,
                created_at=item.created_at,
            )
            for item in items
        ),
        warnings=tuple(warnings),
    )


def work_show_sync(
    payload: WorkShowInput,
    ctx: RuntimeContext | None = None,
) -> WorkShowOutput:
    _ = ctx
    roots = resolve_roots_for_read(payload.project_root)
    if roots is None:
        raise ValueError(f"Work item '{payload.work_id}' not found")
    project_root = roots.project_root
    project_state_dir = roots.project_state_dir
    runtime_state_root = roots.runtime_root

    from meridian.lib.state.reaper import reconcile_spawns

    item = work_store.get_work_item(project_state_dir, payload.work_id)
    if item is None:
        raise ValueError(f"Work item '{payload.work_id}' not found")

    active_session_work_ids = _active_session_work_ids(runtime_state_root)
    associated_spawns = [
        _dashboard_spawn(spawn)
        for spawn in reconcile_spawns(
            project_root,
            runtime_state_root,
            spawn_store.list_spawns(runtime_state_root),
        ).records
        if _associated_with_work_item(
            spawn,
            work_id=item.name,
            active_session_work_ids=active_session_work_ids,
        )
    ]
    associated_spawns.sort(key=lambda spawn: _spawn_id_sort_key(spawn.id))

    wt_path = item.worktree_path
    wt_exists: bool | None = None
    if wt_path is not None:
        wt_exists = Path(wt_path).is_dir()

    return WorkShowOutput(
        name=item.name,
        status=item.status,
        goal=item.goal,
        description=item.description,
        created_at=item.created_at,
        work_dir=work_dir_display(project_root, project_state_dir, item.name),
        task_dir=item.task_dir,
        spawns=tuple(associated_spawns),
        sessions=_work_sessions_for_work_id(
            project_root,
            runtime_state_root,
            item.name,
            include_all=False,
        ),
        worktree_path=wt_path,
        worktree_exists=wt_exists,
        worktree_pending=item.worktree_pending,
    )


def work_sessions_sync(
    payload: WorkSessionsInput,
    ctx: RuntimeContext | None = None,
) -> WorkSessionsOutput:
    roots = resolve_roots_for_read(payload.project_root)
    if roots is None:
        resolved_work_id = payload.work_id.strip()
        if not resolved_work_id:
            raise ValueError("Work ID is required.")
        raise ValueError(f"Work item '{resolved_work_id}' not found")
    project_root = roots.project_root
    project_state_dir = roots.project_state_dir
    runtime_state_root = roots.runtime_root
    resolved_work_id = _resolve_work_id(
        payload_work_id=payload.work_id,
        runtime_root=runtime_state_root,
        ctx=ctx,
    )

    item = work_store.get_work_item(project_state_dir, resolved_work_id)
    if item is None:
        raise ValueError(f"Work item '{resolved_work_id}' not found")

    return WorkSessionsOutput(
        work_id=item.name,
        sessions=_work_sessions_for_work_id(
            project_root,
            runtime_state_root,
            item.name,
            include_all=payload.all or payload.primary,
            primary_only=payload.primary,
        ),
        all=payload.all,
        primary=payload.primary,
    )


work_dashboard = async_from_sync(work_dashboard_sync)
work_list = async_from_sync(work_list_sync)
work_show = async_from_sync(work_show_sync)
work_sessions = async_from_sync(work_sessions_sync)


__all__ = [
    "WorkDashboardInput",
    "WorkDashboardItem",
    "WorkDashboardOutput",
    "WorkDashboardSpawn",
    "WorkListInput",
    "WorkListItem",
    "WorkListOutput",
    "WorkSessionItem",
    "WorkSessionsInput",
    "WorkSessionsOutput",
    "WorkShowInput",
    "WorkShowOutput",
    "work_dashboard",
    "work_dashboard_sync",
    "work_dir_display",
    "work_list",
    "work_list_sync",
    "work_sessions",
    "work_sessions_sync",
    "work_show",
    "work_show_sync",
]
