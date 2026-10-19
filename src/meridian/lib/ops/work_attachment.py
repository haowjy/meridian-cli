"""Work attachment helpers shared by launch, spawn, and work flows."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import structlog

from meridian.lib.core.lifecycle import get_hook_dispatcher
from meridian.lib.state import session_store, work_repository
from meridian.lib.state.paths import resolve_work_scratch_dir_for_project
from meridian.lib.state.work_state import slugify
from meridian.lib.telemetry import emit_telemetry

logger = structlog.get_logger(__name__)


def session_exists(runtime_root: Path, chat_id: str) -> bool:
    normalized = chat_id.strip()
    if not normalized:
        return False
    return session_store.get_session_harness_id(runtime_root, normalized) is not None


def activate_work(
    runtime_root: Path,
    *,
    project_root: Path,
    chat_id: str,
    work_id: str,
) -> bool:
    """Publish an intentional work choice and attach it to a session when possible."""

    normalized = chat_id.strip()
    attached = session_exists(runtime_root, normalized)
    if attached:
        session_store.update_session_work_id(runtime_root, normalized, work_id)
    _dispatch_work_activated(
        project_root=project_root,
        runtime_root=runtime_root,
        work_id=work_id,
    )
    emit_telemetry(
        "work",
        "work.activated",
        scope="ops.work_attachment",
        ids={
            "work_id": work_id,
            **({"chat_id": normalized} if attached else {}),
        },
    )
    return attached


def _dispatch_work_activated(
    *,
    project_root: Path,
    runtime_root: Path,
    work_id: str,
) -> None:
    try:
        dispatcher = get_hook_dispatcher(project_root, runtime_root)
        if dispatcher is None:
            return

        from meridian.lib.hooks.types import HookContext

        dispatcher.fire(
            HookContext(
                event_name="work.activated",
                event_id=uuid4(),
                timestamp=datetime.now(tz=UTC).isoformat(),
                project_root=str(project_root),
                runtime_root=str(runtime_root),
                work_id=work_id,
                work_dir=str(resolve_work_scratch_dir_for_project(project_root, work_id)),
            )
        )
    except Exception:
        logger.exception(
            "Work hook dispatch failed; work activation continues.",
            hook_event="work.activated",
            work_id=work_id,
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
