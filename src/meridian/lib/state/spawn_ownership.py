from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from meridian.lib.state import history_index
from meridian.lib.state.session_identity import spawn_owner_chat_id
from meridian.lib.state.spawn.model import BACKGROUND_LAUNCH_MODE, SpawnRecord
from meridian.lib.state.spawn_observations import LaunchKey


def is_owed_child(row: SpawnRecord) -> bool:
    # A foreground launcher already saw the result; Pi mirrors this rule.
    return row.launch_mode == BACKGROUND_LAUNCH_MODE


def is_live_chat_primary(row: SpawnRecord | None, *, chat_id: str) -> bool:

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
    # A resumed primary must deliver background results owed by earlier generations.
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


def owed_launches(
    index: history_index.HistoryIndex,
    parent_ids: Collection[str],
    *,
    deadline: float | None = None,
) -> tuple[LaunchKey, ...]:
    parents = set(parent_ids)
    moved = index.owed_launches(parents, deadline=deadline)
    children = index.spawns(parent_id=parents, oldest_first=True, deadline=deadline)
    return tuple(
        dict.fromkeys(
            (
                *(LaunchKey(*item) for item in moved),
                *(LaunchKey.of(row) for row in children if is_owed_child(row)),
            )
        )
    )


__all__ = ["is_live_chat_primary", "is_owed_child", "owed_launches", "owned_parent_ids"]
