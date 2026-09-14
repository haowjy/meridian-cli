"""Shared work-item session association helpers."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.state.history_index import HistoryIndex, indexed_spawn_scan


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
        chat_ids.update(HistoryIndex(runtime_root).work_chat_ids(normalized_work_id))
        for spawn in reconcile_spawns(
            project_root,
            runtime_root,
            indexed_spawn_scan(runtime_root, work_id=normalized_work_id),
        ).records:
            if (spawn.work_id or "").strip() != normalized_work_id:
                continue
            chat_id = (spawn.chat_id or "").strip()
            if chat_id:
                chat_ids.add(chat_id)
        return chat_ids

    for record in HistoryIndex(runtime_root).sessions():
        if record.stopped_at is not None or record.record_mode == "historical":
            continue
        if record.active_work_id == normalized_work_id:
            chat_ids.add(record.chat_id)
    for spawn in reconcile_spawns(
        project_root,
        runtime_root,
        indexed_spawn_scan(runtime_root, work_id=normalized_work_id),
    ).records:
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
