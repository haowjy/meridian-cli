"""Shared Claude durable-config metadata persistence helpers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from meridian.lib.core.types import SpawnId


def persist_durable_claude_config_metadata(
    *,
    runtime_root: Path,
    spawn_id: SpawnId | str,
    materialization_root: Path | None,
    update_spawn: Callable[..., object],
    update_session_claude_config_dir: Callable[..., None],
    chat_id: str | None = None,
    get_spawn: Callable[[Path, SpawnId | str], Any | None] | None = None,
) -> None:
    """Persist the durable Claude config dir to spawn + session metadata."""

    if materialization_root is None:
        return

    durable_config_dir = str(materialization_root)
    update_spawn(
        runtime_root,
        spawn_id,
        claude_config_dir=durable_config_dir,
    )

    resolved_chat_id = (chat_id or "").strip() or None
    if resolved_chat_id is None and get_spawn is not None:
        spawn = get_spawn(runtime_root, spawn_id)
        if spawn is not None:
            resolved_chat_id = (spawn.chat_id or "").strip() or None

    if resolved_chat_id:
        update_session_claude_config_dir(
            runtime_root,
            resolved_chat_id,
            claude_config_dir=durable_config_dir,
        )
