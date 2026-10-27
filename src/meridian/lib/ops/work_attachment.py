"""Work attachment helpers shared by launch, spawn, and work flows."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import structlog

from meridian.lib.core.lifecycle import get_hook_dispatcher
from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.launch.session_links import (
    LinkOwner,
    chat_owner,
    link_owners,
    refresh_owner_links,
    work_owners,
)
from meridian.lib.platform.locking import lock_file
from meridian.lib.state import session_store, work_repository, work_store
from meridian.lib.state.paths import RuntimePaths, resolve_project_paths
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


def set_session_work_attachment(
    project_root: Path,
    runtime_root: Path,
    chat_id: str,
    work_id: str | None,
) -> None:
    """Record a chat's work attachment and repoint the links of its execution.

    The single seam for one chat's work item changing. A live primary can move off
    its entry chat — Claude ``/clear`` and ``/resume`` rotate the conversation —
    so every chat of that execution takes the new attachment, and the links stay
    on the entry chat, whose path the launch environment and prompt name.
    Callers own the "does this chat exist" check.
    """

    normalized = chat_id.strip()
    if not normalized or not session_store.session_exists(runtime_root, normalized):
        return
    paths = RuntimePaths.from_root_dir(runtime_root)
    with lock_file(paths.sessions_flock):
        # Read the owners under the same lock as the row writes: a chat observation
        # copies a source row to its new chat while holding this lock, so listing
        # outside it could miss the chat the primary just moved to.
        owners = link_owners(runtime_root, normalized)
        for chat in dict.fromkeys(
            (
                normalized,
                *(owner.chat for owner in owners),
                *(owner.link_chat for owner in owners),
            )
        ):
            session_store.update_session_work_id(runtime_root, chat, work_id)
        # The rows are written before the links: the links are projections of them,
        # and both are under the sessions lock so a concurrent switch cannot
        # interleave them.
        refresh_owner_links(project_root, runtime_root, owners)


def follow_work_item(
    project_root: Path,
    runtime_root: Path,
    work_id: str,
    *,
    rename_to: str | None = None,
    detach: Literal["primary", "all"] | None = None,
    chat_id: str = "",
) -> None:
    """Make every execution attached to one work item follow it.

    The single seam behind ``work done``, ``rename``, ``reopen`` and ``task-dir``:
    one scan of the live sessions attached to the item, then the rows, then the
    links, which are projections of the rows. ``rename_to`` moves the attachment
    to the item's new name; ``detach`` returns those executions to their ambient
    directory instead — a done item is no longer a primary's to edit, and a deleted
    one is nobody's. *chat_id* is the caller, whose session may not be detectable as
    live; it takes part when its row still names the item.
    """

    target_work_id = None if detach == "all" else rename_to or work_id
    with lock_file(RuntimePaths.from_root_dir(runtime_root).sessions_flock):
        # Owners are read under the same lock as the row writes, as everywhere else: a
        # concurrent switch cannot name an execution whose row this pass then misses.
        owners = list(work_owners(runtime_root, work_id))
        caller = _attached_caller(runtime_root, chat_id, work_id)
        if caller is not None and all(
            caller.chat not in (owner.chat, owner.link_chat) for owner in owners
        ):
            owners.append(caller)
        if not owners:
            return
        for owner in owners:
            if detach == "all" or (detach == "primary" and owner.kind == "primary"):
                attachment = None
            else:
                attachment = target_work_id
            for chat in dict.fromkeys((owner.chat, owner.link_chat)):
                session_store.update_session_work_id(runtime_root, chat, attachment)
        refresh_owner_links(project_root, runtime_root, tuple(owners))


def _attached_caller(
    runtime_root: Path,
    chat_id: str,
    work_id: str,
) -> LinkOwner | None:
    """The calling chat as an owner, when its row still names the item."""

    normalized = chat_id.strip()
    if not normalized:
        return None
    owner = chat_owner(runtime_root, normalized)
    if owner is None or (owner.work_id or "") != work_id:
        return None
    return owner


def activate_work(
    project_root: Path,
    chat_id: str,
    work_id: str,
    *,
    runtime_root: Path | None = None,
) -> None:
    """Publish an intentional work choice and attach it to a session when possible."""

    if runtime_root is None:
        from meridian.lib.ops.runtime import resolve_runtime_root

        runtime_root = resolve_runtime_root(project_root)
    normalized = chat_id.strip()
    attached = session_store.session_exists(runtime_root, normalized)
    if attached:
        set_session_work_attachment(project_root, runtime_root, normalized, work_id)
    publish_work_event(
        "work.activated",
        project_root=project_root,
        runtime_root=runtime_root,
        work_id=work_id,
        telemetry_ids={"chat_id": normalized} if attached else None,
    )


def ensure_explicit_work_item(runtime_root: Path, work_id: str) -> str:
    """Create-or-attach an explicitly named work item and return its slug."""

    return work_repository.ensure_work_item_metadata(runtime_root, work_id).name


CURRENT_WORK_ID = "current"


def resolve_active_work_id(
    project_root: Path | None = None,
    runtime_root: Path | None = None,
    *,
    chat_id: str | None = None,
) -> str | None:
    """Return the work item this session acts on, if any.

    The one read behind every "current item": an execution follows its own
    conversation (``/clear`` and ``/resume`` move it), and the env is a launch
    snapshot that only outside a managed run is the whole story. *chat_id* names a
    conversation to read instead of this process's own.
    """

    resolved = ResolvedContext.from_environment(
        explicit_chat_id=chat_id,
        explicit_project_root=project_root,
        explicit_runtime_root=runtime_root,
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
    context_work_id: str | None,
) -> str | None:
    """Materialize explicit ``--work`` or inherited ``--from`` work onto disk."""

    candidate = (explicit_work_id or "").strip() or (context_work_id or "").strip() or None
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
