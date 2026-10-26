"""Work attachment helpers shared by launch, spawn, and work flows."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import structlog

from meridian.lib.core.lifecycle import get_hook_dispatcher
from meridian.lib.platform.locking import lock_file
from meridian.lib.state import session_store, spawn_store, work_repository, work_store
from meridian.lib.state.paths import RuntimePaths, resolve_project_paths
from meridian.lib.state.session_work_link import refresh_session_work_link
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
    """Record a chat's work attachment and repoint the primary's stable work link.

    The single seam for a chat's work item changing. A live primary can move off
    its entry chat — Claude ``/clear`` and ``/resume`` rotate the conversation —
    so every chat of that execution takes the new attachment, and the link stays
    on the entry chat, whose path the launch environment and system prompt name.
    Spawn sessions record the attachment and keep their launch-bound directory.
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
        owners = [
            owner
            for owner in spawn_store.list_live_primaries(runtime_root)
            if owner.chat_id and owner.continue_chat_id == normalized
        ]
        for chat in dict.fromkeys(
            (normalized, *(owner.chat_id for owner in owners if owner.chat_id))
        ):
            session_store.update_session_work_id(runtime_root, chat, work_id)
        # The row is written before the link: the link is a projection of it, and
        # both are under the sessions lock so a concurrent switch cannot interleave
        # them. Only a primary owns a link, and only its entry chat's path is named.
        for owner in owners:
            if owner.chat_id:
                refresh_session_work_link(
                    project_root,
                    runtime_root,
                    owner.chat_id,
                    ambient_spawn_id=owner.id,
                )


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
