from __future__ import annotations

from typing import Any

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import HarnessConnection
from meridian.lib.streaming.spawn_manager import SpawnManager


def active_connection(manager: SpawnManager, spawn_id: SpawnId) -> HarnessConnection[Any] | None:
    """The connection of a spawn the manager still runs, or None once it published."""

    session = manager._sessions.get(spawn_id)
    if session is None or session.terminal_published:
        return None
    return session.connection
