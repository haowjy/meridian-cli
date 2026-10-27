"""Work attachment helpers shared by launch, spawn, and work flows."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import structlog

from meridian.lib.core.lifecycle import get_hook_dispatcher
from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.launch.session_links import Execution, execution_of, publish_chat_links
from meridian.lib.state import session_store, work_repository, work_store
from meridian.lib.state.paths import resolve_project_paths
from meridian.lib.state.work_state import slugify
from meridian.lib.telemetry import emit_telemetry

logger = structlog.get_logger(__name__)

WorkEventName = Literal["work.created", "work.activated", "work.done"]


def publish_work_event(
    event_name: WorkEventName,
    *,
    project_root: Path,
    runtime_root: Path,
    work_id: str,
    telemetry_ids: Mapping[str, str] | None = None,
    data: dict[str, object] | None = None,
) -> None:
    """Publish one work event to hooks and its paired telemetry projection."""

    try:
        dispatcher = get_hook_dispatcher(project_root, runtime_root)
        if dispatcher is not None:
            from meridian.lib.hooks.types import HookContext

            project_state_dir = resolve_project_paths(project_root).root_dir
            dispatcher.fire(
                HookContext(
                    event_name=event_name,
                    event_id=uuid4(),
                    timestamp=datetime.now(tz=UTC).isoformat(),
                    project_root=str(project_root),
                    runtime_root=str(runtime_root),
                    work_id=work_id,
                    work_dir=str(work_store.work_scratch_dir(project_state_dir, work_id)),
                )
            )
    except ValueError as exc:
        logger.error(
            "Work hook dispatch failed; work transition continues.",
            hook_event=event_name,
            work_id=work_id,
            error=str(exc),
        )
    except Exception:
        logger.exception(
            "Work hook dispatch failed; work transition continues.",
            hook_event=event_name,
            work_id=work_id,
        )

    emit_telemetry(
        "work",
        event_name,
        scope="ops.work_attachment",
        ids={"work_id": work_id, **dict(telemetry_ids or {})},
        data=data,
    )


def attach_executions(
    project_root: Path,
    runtime_root: Path,
    executions: Iterable[Execution],
    work_id: str | None,
) -> frozenset[str]:
    """Point executions at a work item, or at their ambient directory with ``None``.

    The one place an execution's attachment changes, and the only session write a
    work command makes: ``done``, ``reopen`` and ``task-dir`` move an item, and its
    own links carry every attached chat along. The two stores are two steps, never
    nested: the item's links come first, in the work store; then, in one session
    epoch, the rows in one batch and the chat links they project. Every chat of an
    execution takes the attachment — a primary can move off its entry chat (Claude
    ``/clear`` and ``/resume`` rotate the conversation) — while the links stay on the
    entry chat, whose path the launch environment and prompt name. Returns the chats
    that now carry the attachment.
    """

    targets = tuple(executions)
    if work_id:
        work_repository.ensure_item_links(resolve_project_paths(project_root).root_dir, work_id)
    with session_store.session_writes(runtime_root):
        attached = session_store.set_work_attachments(
            runtime_root, {chat: work_id for target in targets for chat in target.chats}
        )
        for target in targets:
            # A chat that took no row (unknown or historical) keeps the links it has.
            if target.spawn_id and target.entry_chat in attached:
                publish_chat_links(
                    project_root,
                    runtime_root,
                    target.entry_chat,
                    spawn_id=target.spawn_id,
                    work_id=work_id,
                )
    return attached


def attach_session(
    project_root: Path,
    runtime_root: Path,
    chat_id: str,
    work_id: str | None,
) -> bool:
    """Attach the execution a command on *chat_id* acts for; return whether the chat exists.

    An execution is read from the caller's own spawn row, so this costs no scan of
    the spawn store.
    """

    normalized = chat_id.strip()
    if not normalized:
        return False
    execution = execution_of(
        runtime_root, chat_id=normalized, spawn_id=os.environ.get("MERIDIAN_SPAWN_ID")
    )
    attached = attach_executions(project_root, runtime_root, (execution,), work_id)
    return normalized in attached


def activate_work(
    project_root: Path,
    chat_id: str,
    work_id: str,
    *,
    runtime_root: Path | None = None,
) -> bool:
    """Publish an intentional work choice and attach it to a session when possible.

    Returns whether the chat is a session that took the attachment.
    """

    if runtime_root is None:
        from meridian.lib.ops.runtime import resolve_runtime_root

        runtime_root = resolve_runtime_root(project_root)
    normalized = chat_id.strip()
    attached = attach_session(project_root, runtime_root, normalized, work_id)
    publish_work_event(
        "work.activated",
        project_root=project_root,
        runtime_root=runtime_root,
        work_id=work_id,
        telemetry_ids={"chat_id": normalized} if attached else None,
    )
    return attached


def ensure_explicit_work_item(runtime_root: Path, work_id: str) -> str:
    """Create-or-attach an explicitly named work item and return its slug."""

    return work_repository.ensure_work_item_metadata(runtime_root, work_id).name


CURRENT_WORK_ID = "current"


def resolve_active_work_id(
    project_root: Path | None = None,
    runtime_root: Path | None = None,
    *,
    chat_id: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return the work item this session acts on, if any.

    The one read behind every "current item": an execution follows its own
    conversation (``/clear`` and ``/resume`` move it), and the env is a launch
    snapshot that only outside a managed run is the whole story. *chat_id* names a
    conversation to read instead of this process's own; *environ* describes another
    execution's environment instead of this process's.
    """

    resolved = ResolvedContext.from_environment(
        explicit_chat_id=chat_id,
        explicit_project_root=project_root,
        explicit_runtime_root=runtime_root,
        environ=environ,
    )
    return (resolved.work_id or "").strip() or None


def resolve_work_id_argument(
    work_id: str | None,
    *,
    usage: str,
    project_root: Path | None = None,
    runtime_root: Path | None = None,
) -> str:
    """Resolve an argument that names a work item: a literal ID, `current`, or none.

    An empty argument means the session's current item too, so `work done` acts on
    the item a `work switch` put in place — the work-item analog of git commands
    reading `HEAD` — and no caller has to carry an ID that can go stale.
    """

    resolved = _resolve_work_reference(
        work_id,
        project_root=project_root,
        runtime_root=runtime_root,
    )
    if resolved is None:
        raise ValueError(
            f"No current work item. Pass one (`{usage}`), or run from a session "
            "attached to a work item."
        )
    return resolved


def resolve_current_work_alias(
    work_id: str | None,
    *,
    usage: str,
    project_root: Path | None = None,
    runtime_root: Path | None = None,
) -> str | None:
    """Resolve the `current` alias in an optional argument; an empty one stays empty.

    A launch's `--work` is optional and may inherit, so an empty argument must not
    become the session's item.
    """

    normalized = (work_id or "").strip()
    if normalized != CURRENT_WORK_ID:
        return normalized or None
    return resolve_work_id_argument(
        normalized,
        usage=usage,
        project_root=project_root,
        runtime_root=runtime_root,
    )


def ensure_work_name_is_usable(name: str) -> None:
    """Refuse the one name the `current` alias would shadow."""

    if name == CURRENT_WORK_ID:
        raise ValueError(
            f"'{CURRENT_WORK_ID}' is reserved for the session's current work item. "
            "Pick another name."
        )


def _resolve_work_reference(
    work_id: str | None,
    *,
    project_root: Path | None,
    runtime_root: Path | None,
) -> str | None:
    normalized = (work_id or "").strip()
    if normalized and normalized != CURRENT_WORK_ID:
        return normalized
    return resolve_active_work_id(project_root, runtime_root)


def materialize_launch_work_id(
    runtime_root: Path,
    *,
    explicit_work_id: str | None,
) -> str | None:
    """Materialize an explicit ``--work`` on disk; an inherited one never creates.

    An ambient or ``--from`` work id names an item that already existed when the
    launch resolved it, or none at all (a stale name resolves to no item). Only the
    user naming an item with ``--work`` brings one into being.
    """

    candidate = (explicit_work_id or "").strip() or None
    if candidate is None:
        return None
    return ensure_explicit_work_item(runtime_root, candidate)


def preview_launch_work_id(
    *,
    explicit_work_id: str | None,
    context_work_id: str | None,
) -> str | None:
    """Normalize explicit or inherited work for dry-run without creating it."""

    candidate = (explicit_work_id or "").strip() or (context_work_id or "").strip() or None
    if candidate is None:
        return None
    return slugify(candidate) or None
