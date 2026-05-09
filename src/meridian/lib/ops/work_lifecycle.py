"""Work item lifecycle and attachment mutations."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict

from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.lifecycle import generate_lifecycle_event_id, get_hook_dispatcher
from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.runtime import (
    async_from_sync,
    resolve_chat_id,
    resolve_roots,
    runtime_context,
)
from meridian.lib.ops.work_attachment import set_session_work_attachment
from meridian.lib.ops.work_dashboard import work_dir_display
from meridian.lib.state import session_store, spawn_store, work_store
from meridian.lib.telemetry import emit_telemetry

_NESTED_WORK_WARNING = (
    "Work coordination is primary-owned; nested agents should usually ask the orchestrator "
    "to run this command."
)
logger = structlog.get_logger(__name__)


def _require_work_item(project_state_dir: Path, work_id: str) -> work_store.WorkItem:
    item = work_store.get_work_item(project_state_dir, work_id)
    if item is None:
        raise ValueError(f"Work item '{work_id}' not found")
    return item


def _work_warning(ctx: RuntimeContext | None) -> str | None:
    if runtime_context(ctx).is_nested:
        return _NESTED_WORK_WARNING
    return None


def _merge_warnings(*warnings: str | None) -> str | None:
    parts = [warning.strip() for warning in warnings if warning and warning.strip()]
    if not parts:
        return None
    return "\n".join(parts)


def _active_work_attachment_warning(runtime_root: Path, work_id: str) -> str | None:
    attached_session_ids = session_store.list_active_sessions_for_work_id(runtime_root, work_id)
    active_spawn_ids = [
        spawn.id
        for spawn in spawn_store.list_spawns(runtime_root, filters={"work_id": work_id})
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




def _dispatch_work_hook_event(
    *,
    event_name: Literal["work.started", "work.done"],
    project_root: Path,
    runtime_root: Path,
    project_state_dir: Path,
    work_id: str,
) -> None:
    dispatcher = get_hook_dispatcher(project_root, runtime_root)
    if dispatcher is None:
        return

    try:
        from meridian.lib.hooks.types import HookContext

        dispatcher.fire(
            HookContext(
                event_name=event_name,
                event_id=generate_lifecycle_event_id(work_id, event_name, 0),
                timestamp=datetime.now(tz=UTC).isoformat(),
                project_root=str(project_root),
                runtime_root=str(runtime_root),
                work_id=work_id,
                work_dir=str(work_store.work_scratch_dir(project_state_dir, work_id)),
            )
        )
    except Exception:
        logger.exception(
            "Work hook dispatch failed; work lifecycle transition continues.",
            hook_event=event_name,
            work_id=work_id,
        )


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
    worktree: bool | None = None  # None = use config default


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
    worktree_path: str | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        return ""


class WorkUpdateInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_id: str
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
        return ""


class WorkDoneInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_id: str
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
        return ""


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
        return ""


class WorkRenameInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_id: str
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


def _resolve_worktree_intent(
    *,
    explicit_worktree: bool | None,
    project_root: Path,
) -> bool:
    """Resolve whether to provision a worktree.

    - ``True``:  always create (caller must handle failure)
    - ``False``: never create
    - ``None``:  defer to ``[work] default_worktree`` in project config
    """
    if explicit_worktree is True:
        return True
    if explicit_worktree is False:
        return False
    from meridian.lib.config.settings import load_config  # local to avoid circular import

    config = load_config(project_root)
    return config.work.default_worktree


def _provision_worktree(
    project_state_dir: Path,
    item: work_store.WorkItem,
    project_root: Path,
    *,
    explicit: bool,
    created: bool,
    worktree_base: str | None,
) -> str | None:
    """Create or reuse a git worktree for *item*.

    Returns:
        None on success.
        A non-empty warning string on soft failure (WT-06: config default +
        no git repo).

    Raises:
        ValueError on hard failure:
            - WT-02: explicit ``--worktree`` outside git repo.
            - WT-03 / general: git worktree creation error.

    For newly created work items (``created=True``) any hard failure triggers
    rollback (delete the item) before the exception propagates so the caller
    sees a clean failure.
    """
    from meridian.lib.ops.worktree_ops import (  # local to avoid import-time side effects
        create_worktree,
        detect_git_repo,
        resolve_main_repo_root,
        resolve_worktree_path,
    )

    # 1. Mark provisioning in progress.
    work_store.update_work_item(project_state_dir, item.name, worktree_pending=True)

    # 2. Detect git repo.
    if not detect_git_repo(project_root):
        # Clear pending — nothing changed on disk yet.
        try:
            work_store.update_work_item(project_state_dir, item.name, worktree_pending=False)
        except Exception:
            logger.warning(
                "work_lifecycle.provision_worktree.clear_pending_failed",
                work_id=item.name,
            )
        if explicit:
            # WT-02: hard failure — explicit request, not a git repo.
            if created:
                try:
                    work_store.delete_work_item(project_state_dir, item.name, force=True)
                    logger.info(
                        "work_lifecycle.provision_worktree.rollback_done", work_id=item.name
                    )
                except Exception:
                    logger.exception(
                        "work_lifecycle.provision_worktree.rollback_failed",
                        work_id=item.name,
                    )
            raise ValueError(
                f"Cannot create git worktree for '{item.name}': "
                f"'{project_root}' is not inside a git repository. "
                "Pass --no-worktree to skip worktree creation."
            )
        # WT-06: soft failure — config default, not a git repo.
        return (
            f"Skipped worktree creation for '{item.name}': "
            "project is not inside a git repository."
        )

    # 3. Attempt worktree creation.
    try:
        repo_root = resolve_main_repo_root(project_root)
        if repo_root is None:
            raise ValueError(f"Could not determine git repository root from '{project_root}'.")

        target_path = resolve_worktree_path(repo_root, item.name, worktree_base)
        branch = f"feature/{item.name}"
        result = create_worktree(repo_root, target_path, branch)

        work_store.update_work_item(
            project_state_dir,
            item.name,
            worktree_path=str(result.path),
            worktree_pending=False,
        )
        logger.info(
            "work_lifecycle.provision_worktree.done",
            work_id=item.name,
            worktree_path=str(result.path),
            branch=branch,
            created_new=result.created,
        )
        return None

    except Exception as exc:
        # Clear pending flag.
        try:
            work_store.update_work_item(project_state_dir, item.name, worktree_pending=False)
        except Exception:
            logger.warning(
                "work_lifecycle.provision_worktree.clear_pending_failed",
                work_id=item.name,
            )

        if created:
            # WT-03: rollback the newly created work item.
            try:
                work_store.delete_work_item(project_state_dir, item.name, force=True)
                logger.info("work_lifecycle.provision_worktree.rollback_done", work_id=item.name)
            except Exception:
                logger.exception(
                    "work_lifecycle.provision_worktree.rollback_failed",
                    work_id=item.name,
                )
        else:
            logger.error(
                "work_lifecycle.provision_worktree.failed",
                work_id=item.name,
                error=str(exc),
            )

        raise ValueError(
            f"Failed to create git worktree for '{item.name}': {exc}"
        ) from exc


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
    normalized_work_id = work_store.slugify(payload.label)
    if not normalized_work_id:
        raise ValueError("Work item label must contain at least one letter or number.")

    existing = work_store.get_work_item(project_state_dir, normalized_work_id)
    created = False
    if existing is not None:
        if existing.status == "done":
            raise ValueError(
                f"Work item '{existing.name}' is done. "
                f"Use `meridian work reopen {existing.name}` first."
            )
        item = existing
    else:
        item = work_store.create_work_item(
            project_state_dir,
            payload.label,
            requested_description,
            payload.goal,
        )
        created = True

    # Worktree provisioning before session attachment so that WT-03 rollback
    # (new item deleted on git failure) leaves the session unaffected.
    worktree_warning: str | None = None
    worktree_requested = _resolve_worktree_intent(
        explicit_worktree=payload.worktree,
        project_root=project_root,
    )
    if worktree_requested and (created or item.worktree_path is None):
        from meridian.lib.config.settings import load_config  # local to avoid circular import

        cfg = load_config(project_root)
        worktree_warning = _provision_worktree(
            project_state_dir,
            item,
            project_root,
            explicit=payload.worktree is True,
            created=created,
            worktree_base=cfg.work.worktree_base,
        )
        # Re-read to pick up updated worktree_path / cleared worktree_pending.
        item = work_store.get_work_item(project_state_dir, item.name) or item

    set_session_work_attachment(runtime_state_root, chat_id=chat_id, work_id=item.name)
    _dispatch_work_hook_event(
        event_name="work.started",
        project_root=project_root,
        runtime_root=runtime_state_root,
        project_state_dir=project_state_dir,
        work_id=item.name,
    )
    _emit_work_transition(
        "work.started",
        work_id=item.name,
        data={"status": item.status, "created": created},
    )
    return WorkStartOutput(
        name=item.name,
        status=item.status,
        description=item.description,
        goal=item.goal,
        created_at=item.created_at,
        work_dir=work_dir_display(project_root, project_state_dir, item.name),
        created=created,
        warning=_merge_warnings(warning, worktree_warning),
        worktree_path=item.worktree_path,
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
    runtime_state_root = roots.runtime_root
    current = _require_work_item(project_state_dir, payload.work_id)
    if payload.status == "done":
        attachment_warning = _active_work_attachment_warning(runtime_state_root, payload.work_id)
        item = work_store.archive_work_item(
            project_state_dir,
            payload.work_id,
            description=payload.description,
        )
        _dispatch_work_hook_event(
            event_name="work.done",
            project_root=roots.project_root,
            runtime_root=runtime_state_root,
            project_state_dir=project_state_dir,
            work_id=item.name,
        )
        _emit_work_transition(
            "work.done",
            work_id=item.name,
            data={"status": item.status},
        )
        return WorkUpdateOutput(
            name=item.name,
            status=item.status,
            warning=_merge_warnings(warning, attachment_warning),
        )
    if current.status == "done" and payload.status is not None:
        raise ValueError(
            f"Work item '{payload.work_id}' is done. "
            f"Use `meridian work reopen {payload.work_id}` first."
        )
    item = work_store.update_work_item(
        project_state_dir,
        payload.work_id,
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
    project_state_dir = roots.project_state_dir
    runtime_state_root = roots.runtime_root
    attachment_warning = _active_work_attachment_warning(runtime_state_root, payload.work_id)
    item = work_store.archive_work_item(project_state_dir, payload.work_id)
    _dispatch_work_hook_event(
        event_name="work.done",
        project_root=roots.project_root,
        runtime_root=runtime_state_root,
        project_state_dir=project_state_dir,
        work_id=item.name,
    )
    _emit_work_transition(
        "work.done",
        work_id=item.name,
        data={"status": item.status},
    )
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
    project_state_dir = roots.project_state_dir
    item, had_artifacts = work_store.delete_work_item(
        project_state_dir,
        payload.work_id,
        force=payload.force,
    )
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
    project_state_dir = resolve_roots(payload.project_root).project_state_dir
    item = work_store.reopen_work_item(project_state_dir, payload.work_id)
    _emit_work_transition(
        "work.reopened",
        work_id=item.name,
        data={"status": item.status},
    )
    return WorkReopenOutput(name=item.name, status=item.status, warning=warning)


def work_switch_sync(
    payload: WorkSwitchInput,
    ctx: RuntimeContext | None = None,
) -> WorkSwitchOutput:
    warning = _work_warning(ctx)
    roots = resolve_roots(payload.project_root)
    project_state_dir = roots.project_state_dir
    runtime_state_root = roots.runtime_root
    item = _require_work_item(project_state_dir, payload.work_id)
    chat_id = resolve_chat_id(payload_chat_id=payload.chat_id, ctx=runtime_context(ctx))
    updated = set_session_work_attachment(runtime_state_root, chat_id=chat_id, work_id=item.name)
    message = (
        f"Active work item: {item.name}"
        if updated
        else f"Work item ready: {item.name} (no active session to update)"
    )
    return WorkSwitchOutput(work_id=item.name, message=message, warning=warning)


def work_rename_sync(
    payload: WorkRenameInput,
    ctx: RuntimeContext | None = None,
) -> WorkRenameOutput:
    warning = _work_warning(ctx)
    roots = resolve_roots(payload.project_root)
    project_state_dir = roots.project_state_dir
    runtime_state_root = roots.runtime_root
    old_name = payload.work_id
    _require_work_item(project_state_dir, old_name)
    item = work_store.rename_work_item(project_state_dir, old_name, payload.new_name)

    for spawn in spawn_store.list_spawns(runtime_state_root, filters={"work_id": old_name}):
        if spawn.kind == "child":
            spawn_store.update_spawn(runtime_state_root, spawn.id, work_id=item.name)

    chat_id = resolve_chat_id(payload_chat_id=payload.chat_id, ctx=runtime_context(ctx))
    current_work_id = session_store.get_session_active_work_id(runtime_state_root, chat_id)
    if current_work_id == old_name:
        set_session_work_attachment(runtime_state_root, chat_id=chat_id, work_id=item.name)

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
    runtime_root = resolve_roots(payload.project_root).runtime_root
    chat_id = resolve_chat_id(payload_chat_id=payload.chat_id, ctx=runtime_context(ctx))
    updated = set_session_work_attachment(
        runtime_root,
        chat_id=chat_id,
        work_id=None,
    )
    message = "Cleared active work item." if updated else "No active session; nothing to clear."
    return WorkClearOutput(message=message, warning=warning)


work_start = async_from_sync(work_start_sync)
work_update = async_from_sync(work_update_sync)
work_done = async_from_sync(work_done_sync)
work_delete = async_from_sync(work_delete_sync)
work_reopen = async_from_sync(work_reopen_sync)
work_switch = async_from_sync(work_switch_sync)
work_rename = async_from_sync(work_rename_sync)
work_clear = async_from_sync(work_clear_sync)


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
    "work_update",
    "work_update_sync",
]
