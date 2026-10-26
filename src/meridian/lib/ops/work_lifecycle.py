"""Work item lifecycle and attachment mutations."""

from __future__ import annotations

from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict

from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.core.util import FormatContext
from meridian.lib.launch.session_links import refresh_primary_links
from meridian.lib.ops.context import resolve_active_work_scope
from meridian.lib.ops.runtime import (
    ResolvedRoots,
    async_from_sync,
    resolve_chat_id,
    resolve_roots,
    runtime_context,
)
from meridian.lib.ops.work_attachment import (
    activate_work,
    ensure_work_name_is_usable,
    publish_work_event,
    resolve_work_id_argument,
    set_session_work_attachment,
)
from meridian.lib.ops.work_dashboard import work_dir_display
from meridian.lib.state import session_store, spawn_store, work_repository, work_store
from meridian.lib.state.spawn_status import overlay_derived
from meridian.lib.state.work_scope import WorkScope
from meridian.lib.state.work_state import WorkItem, slugify
from meridian.lib.telemetry import emit_telemetry

_NESTED_WORK_WARNING = (
    "Work coordination is primary-owned; nested agents should usually ask the orchestrator "
    "to run this command."
)
logger = structlog.get_logger(__name__)


def _validate_task_dir_path(task_dir: str) -> Path:
    resolved_task_dir = Path(task_dir).expanduser().resolve()
    if not resolved_task_dir.exists():
        raise ValueError(f"task_dir does not exist: {resolved_task_dir}")
    if not resolved_task_dir.is_dir():
        raise ValueError(f"task_dir is not a directory: {resolved_task_dir}")
    return resolved_task_dir


def _require_work_item(project_state_dir: Path, work_id: str) -> WorkItem:
    item = work_store.get_work_item(project_state_dir, work_id)
    if item is None:
        raise ValueError(f"Work item '{work_id}' not found")
    return item


def _resolve_work_item(
    roots: ResolvedRoots,
    work_id: str | None,
    *,
    command: str,
) -> WorkItem:
    """Resolve a command's work argument, defaulting to the session's current item."""

    return _require_work_item(
        roots.project_state_dir,
        resolve_work_id_argument(
            work_id,
            usage=f"meridian work {command} <work_id>",
            project_root=roots.project_root,
            runtime_root=roots.runtime_root,
        ),
    )


def _work_warning(ctx: RuntimeContext | None) -> str | None:
    if runtime_context(ctx).is_nested:
        return _NESTED_WORK_WARNING
    return None


def _merge_warnings(*warnings: str | None) -> str | None:
    parts = [warning.strip() for warning in warnings if warning and warning.strip()]
    if not parts:
        return None
    return "\n".join(parts)


def work_scope_label(scope: WorkScope) -> str:
    """Human-readable label for a work scope in user-facing messages."""

    if scope.is_durable:
        return f"work item '{scope.identifier}'"
    if scope.identifier:
        return f"spawn-local work area ({scope.identifier})"
    return "the current scope"


def format_leave_scope_warning(scope: WorkScope, new_target: str) -> str | None:
    """Return a leave-scope warning when artifacts would be left behind."""

    count = scope.count_artifacts()
    if count <= 0:
        return None
    noun = "artifact" if count == 1 else "artifacts"
    return (
        f"{count} {noun} in {work_scope_label(scope)} won't follow you to {new_target}; "
        "move what you want to keep."
    )


def _leave_scope_warning(
    *,
    project_root: Path,
    project_state_dir: Path,
    runtime_root: Path,
    chat_id: str,
    new_target: str,
) -> str | None:
    outgoing_scope = resolve_active_work_scope(
        project_root,
        runtime_root,
        chat_id=chat_id,
    )
    if outgoing_scope is None:
        return None

    incoming_dir = work_store.work_scratch_dir(project_state_dir, new_target).resolve()
    if outgoing_scope.root.resolve() == incoming_dir:
        return None

    return format_leave_scope_warning(outgoing_scope, new_target)


def _active_work_attachment_warning(runtime_root: Path, work_id: str) -> str | None:
    attached_session_ids = session_store.list_active_sessions_for_work_id(runtime_root, work_id)
    active_spawn_ids = [
        spawn.id
        for spawn in overlay_derived(
            runtime_root,
            spawn_store.list_spawns(runtime_root, work_id=work_id).records,
        )
        if spawn.kind != "primary" and is_active_spawn_status(spawn.status)
    ]
    warnings: list[str] = []
    if attached_session_ids:
        warnings.append(f"session(s): {', '.join(attached_session_ids)}")
    if active_spawn_ids:
        warnings.append(f"active spawn(s): {', '.join(active_spawn_ids)}")
    if not warnings:
        return None
    return "Work item marked done while still referenced by " + "; ".join(warnings) + "."


def _detach_primary_sessions_from_work(
    project_root: Path,
    runtime_root: Path,
    work_id: str,
) -> None:
    """Return attached primary sessions to their ambient work directory."""

    for row in spawn_store.list_live_primaries(runtime_root):
        chat_id = row.continue_chat_id
        if not chat_id:
            continue
        if session_store.get_session_active_work_id(runtime_root, chat_id) == work_id:
            set_session_work_attachment(project_root, runtime_root, chat_id, None)


def _archive_work_item(
    roots: ResolvedRoots,
    current: WorkItem,
    *,
    description: str | None = None,
) -> tuple[WorkItem, str | None]:
    """Archive the item, detach its primary sessions, and report what still holds it."""

    item = work_repository.archive_work_item(
        roots.project_state_dir,
        current.name,
        description=description,
    )
    _detach_primary_sessions_from_work(roots.project_root, roots.runtime_root, item.name)
    publish_work_event(
        "work.done",
        project_root=roots.project_root,
        runtime_root=roots.runtime_root,
        work_id=item.name,
        data={"status": item.status},
    )
    # Reported after detaching: a primary the archive just detached is no longer
    # a reference, so naming it would contradict the session's new attachment.
    return item, _active_work_attachment_warning(roots.runtime_root, item.name)


def _emit_work_transition(
    event: str,
    *,
    work_id: str,
    data: dict[str, object] | None = None,
) -> None:
    emit_telemetry(
        "work",
        event,
        scope="ops.work_lifecycle",
        ids={"work_id": work_id},
        data=data,
    )


class WorkStartInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    description: str = ""
    goal: str | None = None
    chat_id: str = ""
    project_root: str | None = None
    task_dir: str | None = None


class WorkStartOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    status: str
    description: str
    goal: str | None = None
    created_at: str
    work_dir: str
    created: bool = True
    warning: str | None = None
    task_dir: str | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        return ""


class WorkUpdateInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_id: str = ""
    status: str | None = None
    description: str | None = None
    goal: str | None = None
    project_root: str | None = None


class WorkUpdateOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    status: str
    warning: str | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        if self.warning:
            return self.warning
        return ""


class WorkDoneInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_id: str = ""
    project_root: str | None = None


class WorkDeleteInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_id: str
    force: bool = False
    project_root: str | None = None


class WorkDeleteOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    had_artifacts: bool
    deleted: bool
    warning: str = ""

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        lines: list[str] = []
        if not self.deleted:
            lines.append(f"Work item '{self.name}' has artifacts. Use --force to delete.")
        elif self.had_artifacts:
            lines.append(f"Deleted work item '{self.name}' and its artifacts.")
        else:
            lines.append(f"Deleted work item '{self.name}'.")
        if self.warning:
            lines.append(self.warning)
        return "\n".join(lines)


class WorkSwitchInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_id: str
    chat_id: str = ""
    project_root: str | None = None


class WorkSwitchOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_id: str
    message: str
    warning: str | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        return f"Active work item: {self.work_id}"


class WorkReopenInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_id: str
    project_root: str | None = None


class WorkReopenOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    status: str
    warning: str | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        if self.warning:
            return self.warning
        return ""


class WorkRenameInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_id: str = ""
    new_name: str
    chat_id: str = ""
    project_root: str | None = None


class WorkRenameOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    old_name: str
    new_name: str
    changed: bool = True
    warning: str | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        return ""


class WorkClearInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    chat_id: str = ""
    project_root: str | None = None


class WorkClearOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    message: str
    warning: str | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        return ""


class WorkTaskDirInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_dir: str | None = None
    clear: bool = False
    chat_id: str = ""
    project_root: str | None = None


class WorkTaskDirOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_dir: str
    warning: str | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        return self.task_dir


def work_start_sync(
    payload: WorkStartInput,
    ctx: RuntimeContext | None = None,
) -> WorkStartOutput:
    warning = _work_warning(ctx)
    roots = resolve_roots(payload.project_root)
    project_root = roots.project_root
    project_state_dir = roots.project_state_dir
    runtime_state_root = roots.runtime_root
    chat_id = resolve_chat_id(payload_chat_id=payload.chat_id, ctx=runtime_context(ctx))
    requested_description = payload.description.strip()
    normalized_work_id = slugify(payload.label)
    if not normalized_work_id:
        raise ValueError("Work item label must contain at least one letter or number.")
    ensure_work_name_is_usable(normalized_work_id)
    slug_warning: str | None = None
    if normalized_work_id != payload.label.strip():
        slug_warning = f"Normalized '{payload.label.strip()}' to '{normalized_work_id}'."

    normalized_task_dir = (payload.task_dir or "").strip() or None
    resolved_task_dir: Path | None = None
    if normalized_task_dir is not None:
        resolved_task_dir = _validate_task_dir_path(normalized_task_dir)

    existing = work_store.get_work_item(project_state_dir, normalized_work_id)
    created = False
    reattach_warning: str | None = None
    if existing is not None:
        if existing.status == "done":
            # Treat `work start <name>` on an archived item as an implicit reopen —
            # the user's intent is "I want to work on this" regardless of prior state.
            item = work_repository.reopen_work_item(project_state_dir, existing.name)
            reattach_warning = f"Work item '{item.name}' was archived; reopened automatically."
        else:
            item = existing
            reattach_warning = (
                f"Work item '{item.name}' already exists; attaching to existing item."
            )
    else:
        item = work_repository.create_work_item(
            project_state_dir,
            payload.label,
            requested_description,
            payload.goal,
        )
        created = True
    task_dir_warning: str | None = None
    if resolved_task_dir is not None:
        item = work_repository.update_work_item_task_dir(
            project_state_dir,
            item.name,
            task_dir=resolved_task_dir.as_posix(),
        )
        task_dir_warning = f"Set task_dir to {resolved_task_dir.as_posix()}."

    leave_warning = _leave_scope_warning(
        project_root=project_root,
        project_state_dir=project_state_dir,
        runtime_root=runtime_state_root,
        chat_id=chat_id,
        new_target=item.name,
    )
    if created:
        publish_work_event(
            "work.created",
            project_root=project_root,
            runtime_root=runtime_state_root,
            work_id=item.name,
            data={"status": item.status},
        )
    activate_work(
        project_root,
        chat_id,
        item.name,
        runtime_root=runtime_state_root,
    )
    return WorkStartOutput(
        name=item.name,
        status=item.status,
        description=item.description,
        goal=item.goal,
        created_at=item.created_at,
        work_dir=work_dir_display(project_root, project_state_dir, item.name),
        created=created,
        warning=_merge_warnings(
            warning,
            slug_warning,
            reattach_warning,
            task_dir_warning,
            leave_warning,
        ),
        task_dir=item.task_dir,
    )


def work_update_sync(
    payload: WorkUpdateInput,
    ctx: RuntimeContext | None = None,
) -> WorkUpdateOutput:
    warning = _work_warning(ctx)
    if payload.status is None and payload.description is None and payload.goal is None:
        raise ValueError("Nothing to update. Pass --status, --description, and/or --goal.")
    roots = resolve_roots(payload.project_root)
    project_state_dir = roots.project_state_dir
    current = _resolve_work_item(roots, payload.work_id, command="update")
    if payload.status == "done":
        item, attachment_warning = _archive_work_item(
            roots,
            current,
            description=payload.description,
        )
        return WorkUpdateOutput(
            name=item.name,
            status=item.status,
            warning=_merge_warnings(warning, attachment_warning),
        )
    if current.status == "done" and payload.status is not None:
        raise ValueError(
            f"Work item '{current.name}' is done. "
            f"Use `meridian work reopen {current.name}` first."
        )
    item = work_repository.update_work_item(
        project_state_dir,
        current.name,
        status=payload.status,
        description=payload.description,
        goal=payload.goal,
    )
    _emit_work_transition(
        "work.updated",
        work_id=item.name,
        data={"status": item.status},
    )
    return WorkUpdateOutput(name=item.name, status=item.status, warning=warning)


def work_done_sync(
    payload: WorkDoneInput,
    ctx: RuntimeContext | None = None,
) -> WorkUpdateOutput:
    nested_warning = _work_warning(ctx)
    roots = resolve_roots(payload.project_root)
    current = _resolve_work_item(roots, payload.work_id, command="done")
    item, attachment_warning = _archive_work_item(roots, current)
    return WorkUpdateOutput(
        name=item.name,
        status=item.status,
        warning=_merge_warnings(nested_warning, attachment_warning),
    )


def work_delete_sync(
    payload: WorkDeleteInput,
    ctx: RuntimeContext | None = None,
) -> WorkDeleteOutput:
    nested_warning = _work_warning(ctx)
    roots = resolve_roots(payload.project_root)
    item, had_artifacts = work_repository.delete_work_item(
        roots.project_state_dir,
        _resolve_work_item(roots, payload.work_id, command="delete").name,
        force=payload.force,
    )
    _detach_primary_sessions_from_work(roots.project_root, roots.runtime_root, item.name)
    _emit_work_transition(
        "work.deleted",
        work_id=item.name,
        data={"status": item.status, "had_artifacts": had_artifacts},
    )
    return WorkDeleteOutput(
        name=item.name,
        had_artifacts=had_artifacts,
        deleted=True,
        warning=nested_warning or "",
    )


def work_reopen_sync(
    payload: WorkReopenInput,
    ctx: RuntimeContext | None = None,
) -> WorkReopenOutput:
    warning = _work_warning(ctx)
    roots = resolve_roots(payload.project_root)
    project_state_dir = roots.project_state_dir
    item = work_repository.reopen_work_item(
        project_state_dir,
        _resolve_work_item(roots, payload.work_id, command="reopen").name,
    )
    _emit_work_transition(
        "work.reopened",
        work_id=item.name,
        data={"status": item.status},
    )
    return WorkReopenOutput(
        name=item.name,
        status=item.status,
        warning=warning,
    )


def work_switch_sync(
    payload: WorkSwitchInput,
    ctx: RuntimeContext | None = None,
) -> WorkSwitchOutput:
    warning = _work_warning(ctx)
    roots = resolve_roots(payload.project_root)
    project_state_dir = roots.project_state_dir
    runtime_state_root = roots.runtime_root
    item = _resolve_work_item(roots, payload.work_id, command="switch")
    chat_id = resolve_chat_id(payload_chat_id=payload.chat_id, ctx=runtime_context(ctx))
    leave_warning = _leave_scope_warning(
        project_root=roots.project_root,
        project_state_dir=project_state_dir,
        runtime_root=runtime_state_root,
        chat_id=chat_id,
        new_target=item.name,
    )
    updated = session_store.session_exists(runtime_state_root, chat_id)
    activate_work(
        roots.project_root,
        chat_id,
        item.name,
        runtime_root=runtime_state_root,
    )
    message = (
        f"Active work item: {item.name}"
        if updated
        else f"Work item ready: {item.name} (no active session to update)"
    )
    return WorkSwitchOutput(
        work_id=item.name,
        message=message,
        warning=_merge_warnings(warning, leave_warning),
    )


def work_rename_sync(
    payload: WorkRenameInput,
    ctx: RuntimeContext | None = None,
) -> WorkRenameOutput:
    warning = _work_warning(ctx)
    roots = resolve_roots(payload.project_root)
    project_state_dir = roots.project_state_dir
    runtime_state_root = roots.runtime_root
    old_name = _resolve_work_item(roots, payload.work_id, command="rename").name

    new_slug = slugify(payload.new_name)
    if not new_slug or new_slug != payload.new_name:
        raise ValueError(
            f"Invalid work item name '{payload.new_name}'. "
            f"Use a slug (lowercase, hyphens, no spaces) — e.g. '{new_slug or 'my-feature'}'."
        )
    ensure_work_name_is_usable(new_slug)

    item = work_repository.rename_work_item(project_state_dir, old_name, new_slug)

    for spawn in spawn_store.list_spawns(runtime_state_root, work_id=old_name).records:
        if spawn.kind == "child":
            spawn_store.update_spawn(runtime_state_root, spawn.id, work_id=item.name)

    chat_id = resolve_chat_id(payload_chat_id=payload.chat_id, ctx=runtime_context(ctx))
    # Every session attached to the old name follows the item's directory, not just
    # the caller: the stable work link would otherwise dangle after the rename.
    attached_chat_ids = set(
        session_store.list_active_sessions_for_work_id(runtime_state_root, old_name)
    )
    if (
        chat_id
        and session_store.session_exists(runtime_state_root, chat_id)
        and session_store.get_session_active_work_id(runtime_state_root, chat_id) == old_name
    ):
        attached_chat_ids.add(chat_id)
    for attached_chat_id in sorted(attached_chat_ids):
        set_session_work_attachment(
            roots.project_root,
            runtime_state_root,
            attached_chat_id,
            item.name,
        )

    _emit_work_transition(
        "work.renamed",
        work_id=item.name,
        data={"old_name": old_name, "new_name": item.name, "status": item.status},
    )
    return WorkRenameOutput(
        old_name=old_name,
        new_name=item.name,
        changed=old_name != item.name,
        warning=warning,
    )


def work_clear_sync(
    payload: WorkClearInput,
    ctx: RuntimeContext | None = None,
) -> WorkClearOutput:
    warning = _work_warning(ctx)
    roots = resolve_roots(payload.project_root)
    runtime_root = roots.runtime_root
    chat_id = resolve_chat_id(payload_chat_id=payload.chat_id, ctx=runtime_context(ctx))
    updated = session_store.session_exists(runtime_root, chat_id)
    if updated:
        set_session_work_attachment(roots.project_root, runtime_root, chat_id, None)
    message = "Cleared active work item." if updated else "No active session; nothing to clear."
    return WorkClearOutput(message=message, warning=warning)


def work_task_dir_sync(
    payload: WorkTaskDirInput,
    ctx: RuntimeContext | None = None,
) -> WorkTaskDirOutput:
    warning = _work_warning(ctx)
    if payload.clear and payload.task_dir is not None:
        raise ValueError("Cannot pass both --clear and <path>.")
    roots = resolve_roots(payload.project_root)
    project_root = roots.project_root
    runtime_root = roots.runtime_root
    project_state_dir = roots.project_state_dir
    chat_id = resolve_chat_id(payload_chat_id=payload.chat_id, ctx=runtime_context(ctx))
    active_work_id = session_store.get_session_active_work_id(runtime_root, chat_id)

    if payload.task_dir is None and not payload.clear:
        if not active_work_id:
            return WorkTaskDirOutput(task_dir=project_root.as_posix(), warning=warning)
        item = work_store.get_active_work_item(project_state_dir, active_work_id)
        if item is None or item.task_dir is None:
            return WorkTaskDirOutput(task_dir=project_root.as_posix(), warning=warning)
        return WorkTaskDirOutput(task_dir=item.task_dir, warning=warning)

    if not active_work_id:
        raise ValueError("No active work item. Start or switch to a work item first.")
    item = _require_work_item(project_state_dir, active_work_id)

    if payload.clear:
        updated = work_repository.update_work_item_task_dir(
            project_state_dir,
            item.name,
            task_dir=None,
        )
    else:
        requested_task_dir = (payload.task_dir or "").strip()
        if not requested_task_dir:
            raise ValueError("task_dir path is empty")
        resolved_task_dir = _validate_task_dir_path(requested_task_dir)
        updated = work_repository.update_work_item_task_dir(
            project_state_dir,
            item.name,
            task_dir=resolved_task_dir.as_posix(),
        )

    resolved_task_dir = updated.task_dir or project_root.as_posix()
    # The task link is a projection of this setting, and every attached session
    # edits the item's checkout, so each one's link follows it.
    attached_chat_ids = set(
        session_store.list_active_sessions_for_work_id(runtime_root, updated.name)
    )
    if chat_id:
        attached_chat_ids.add(chat_id)
    for attached_chat_id in sorted(attached_chat_ids):
        refresh_primary_links(project_root, runtime_root, attached_chat_id)
    _emit_work_transition(
        "work.task_dir_updated",
        work_id=updated.name,
        data={"task_dir": resolved_task_dir},
    )
    return WorkTaskDirOutput(task_dir=resolved_task_dir, warning=warning)


work_start = async_from_sync(work_start_sync)
work_update = async_from_sync(work_update_sync)
work_done = async_from_sync(work_done_sync)
work_delete = async_from_sync(work_delete_sync)
work_reopen = async_from_sync(work_reopen_sync)
work_switch = async_from_sync(work_switch_sync)
work_rename = async_from_sync(work_rename_sync)
work_clear = async_from_sync(work_clear_sync)
work_task_dir = async_from_sync(work_task_dir_sync)


__all__ = [
    "WorkClearInput",
    "WorkClearOutput",
    "WorkDeleteInput",
    "WorkDeleteOutput",
    "WorkDoneInput",
    "WorkRenameInput",
    "WorkRenameOutput",
    "WorkReopenInput",
    "WorkReopenOutput",
    "WorkStartInput",
    "WorkStartOutput",
    "WorkSwitchInput",
    "WorkSwitchOutput",
    "WorkTaskDirInput",
    "WorkTaskDirOutput",
    "WorkUpdateInput",
    "WorkUpdateOutput",
    "work_clear",
    "work_clear_sync",
    "work_delete",
    "work_delete_sync",
    "work_done",
    "work_done_sync",
    "work_rename",
    "work_rename_sync",
    "work_reopen",
    "work_reopen_sync",
    "work_start",
    "work_start_sync",
    "work_switch",
    "work_switch_sync",
    "work_task_dir",
    "work_task_dir_sync",
    "work_update",
    "work_update_sync",
]
