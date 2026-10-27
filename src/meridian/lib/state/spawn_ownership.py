"""Which parents' direct children one delivery caller may own.

Both result surfaces share these rules: ``spawn collect`` decides what the CLI may
deliver, and a Pi launch hands the same parent set to its spawn-watch extension,
which mirrors :func:`is_owed_child` as ``isBackgroundLaunch``.
"""

from __future__ import annotations

from pathlib import Path

from meridian.lib.state import history_index
from meridian.lib.state.session_identity import spawn_owner_chat_id
from meridian.lib.state.spawn.model import BACKGROUND_LAUNCH_MODE, SpawnRecord


def is_owed_child(row: SpawnRecord) -> bool:
    """Return whether one direct child's result is still owed to its launcher.

    A launcher that waited for a foreground child already saw that result, so only
    a background child outlives the call that started it.
    """

    return row.launch_mode == BACKGROUND_LAUNCH_MODE


def is_live_chat_primary(row: SpawnRecord | None, *, chat_id: str) -> bool:
    """Return whether ``row`` is this chat's live primary generation."""

    return (
        row is not None
        and row.record_mode == "live"
        and row.kind == "primary"
        and spawn_owner_chat_id(row) == chat_id
    )


def owned_parent_ids(
    runtime_root: Path,
    current: SpawnRecord | None,
    *,
    chat_id: str,
    caller_spawn_id: str,
    deadline: float | None = None,
) -> tuple[str, ...]:
    """Return the parents whose direct children the caller may owe.

    ``current`` is the caller's already-read row. A live primary owns the direct
    children of every primary generation of its chat: a crashed generation's
    background children outlive it, and the primary that resumes the chat is the
    one that must deliver them. Anyone else owns only its own direct children. A
    caller that is absent, not live, or a primary of another chat owns nothing.
    """

    if current is not None and current.record_mode == "live" and current.kind != "primary":
        return (caller_spawn_id,)
    if not is_live_chat_primary(current, chat_id=chat_id):
        return ()
    generations = history_index.HistoryIndex(runtime_root).spawns(
        owner_chat_id=chat_id,
        kind="primary",
        oldest_first=True,
        deadline=deadline,
    )
    return tuple(dict.fromkeys((*(row.id for row in generations), caller_spawn_id)))


__all__ = ["is_live_chat_primary", "is_owed_child", "owned_parent_ids"]
