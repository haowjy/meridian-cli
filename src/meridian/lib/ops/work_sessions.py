"""Shared work-item session association helpers."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.state import session_store, spawn_store


def work_session_chat_ids(
    project_root: Path,
    runtime_root: Path,
    work_id: str,
    *,
    include_all: bool,
) -> set[str]:
    """Resolve chat IDs associated with a work item.

    ``include_all=True`` includes historical session attachments and stopped
    child spawns. ``include_all=False`` mirrors "active now" semantics.
    """

    normalized_work_id = work_id.strip()
    if not normalized_work_id:
        return set()

    from meridian.lib.state.reaper import reconcile_spawns

    chat_ids: set[str] = set()
    if include_all:
        chat_ids.update(
            session_store.chat_ids_ever_attached_to_work(runtime_root, normalized_work_id)
        )
        for spawn in reconcile_spawns(
            project_root,
            runtime_root,
            spawn_store.list_spawns(runtime_root),
        ):
            if (spawn.work_id or "").strip() != normalized_work_id:
                continue
            chat_id = (spawn.chat_id or "").strip()
            if chat_id:
                chat_ids.add(chat_id)
        return chat_ids

    for record in session_store.list_active_session_records(runtime_root):
        if record.active_work_id == normalized_work_id:
            chat_ids.add(record.chat_id)
    for spawn in reconcile_spawns(
        project_root,
        runtime_root,
        spawn_store.list_spawns(runtime_root),
    ):
        if spawn.kind == "primary":
            continue
        if not is_active_spawn_status(spawn.status):
            continue
        if (spawn.work_id or "").strip() != normalized_work_id:
            continue
        chat_id = (spawn.chat_id or "").strip()
        if chat_id:
            chat_ids.add(chat_id)
    return chat_ids


__all__ = ["work_session_chat_ids"]
