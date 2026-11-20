from __future__ import annotations

from pathlib import Path

from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.state import session_store
from meridian.lib.state.spawn.model import SpawnRecord


def active_session_work_ids(runtime_root: Path) -> dict[str, str]:
    """Map each active session's chat id to the work item it is attached to."""
    attached: dict[str, str] = {}
    for record in session_store.list_active_session_records(runtime_root):
        active_work_id = (record.active_work_id or "").strip()
        if active_work_id:
            attached[record.chat_id] = active_work_id
    return attached


def spawn_work_id(spawn: SpawnRecord, active_work_ids: dict[str, str]) -> str | None:
    """Return the work item a spawn belongs to, or None.

    A primary spawn's attachment is its session's, so it belongs to a work item only
    while the spawn is active. Any other spawn belongs to its recorded `work_id`.
    """
    if spawn.kind == "primary":
        chat_id = (spawn.chat_id or "").strip()
        if not chat_id or not is_active_spawn_status(spawn.status):
            return None
        return active_work_ids.get(chat_id)
    return (spawn.work_id or "").strip() or None
