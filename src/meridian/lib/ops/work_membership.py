from __future__ import annotations

from pathlib import Path

from meridian.lib.core.domain import ACTIVE_SPAWN_STATUSES, is_active_spawn_status
from meridian.lib.state import session_store
from meridian.lib.state.history_index import HistoryIndex
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_status import overlay_derived
from meridian.lib.state.spawn_store import SpawnScan


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


def active_spawns(runtime_root: Path) -> list[SpawnRecord]:
    """Every spawn currently active, with derived status applied."""
    rows = HistoryIndex(runtime_root).spawns(status=set(ACTIVE_SPAWN_STATUSES))
    overlaid = overlay_derived(runtime_root, SpawnScan.ordered(rows, ()).records)
    return [row for row in overlaid if is_active_spawn_status(row.status)]


def work_spawns(runtime_root: Path, work_id: str) -> list[SpawnRecord]:
    """Every spawn associated with a work item, with derived status applied.

    A non-primary spawn is indexed by its recorded work id. An active primary is
    attached through its session, so it comes from the active set instead; the
    membership rule decides which candidates actually belong to the item.
    """
    index = HistoryIndex(runtime_root)
    candidates = {row.id: row for row in index.spawns(work_id=work_id)}
    candidates.update(
        (row.id, row) for row in index.spawns(kind="primary", status=set(ACTIVE_SPAWN_STATUSES))
    )
    rows = overlay_derived(runtime_root, SpawnScan.ordered(tuple(candidates.values()), ()).records)
    active_work_ids = active_session_work_ids(runtime_root)
    return [row for row in rows if spawn_work_id(row, active_work_ids) == work_id]
