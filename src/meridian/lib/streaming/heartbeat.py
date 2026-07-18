"""Heartbeat helpers shared by launch runners."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from meridian.lib.core.types import SpawnId
from meridian.lib.state import paths


async def heartbeat_loop(
    runtime_root: Path,
    spawn_id: SpawnId,
    interval: float = 30.0,
    touch: Callable[[Path, SpawnId], None] | None = None,
) -> None:
    """Touch the per-spawn heartbeat sentinel on a fixed interval."""

    sentinel = paths.heartbeat_path(runtime_root, spawn_id)
    while True:
        try:
            if touch is None:
                sentinel.touch()
            else:
                touch(runtime_root, spawn_id)
        except FileNotFoundError:
            return
        await asyncio.sleep(interval)
