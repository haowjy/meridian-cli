"""Disk-state observer for Pi quiescence.

Pi background-work coordination is file-backed: spawn records under ``spawns/`` and
managed bash records under ``pi-bash/<spawn-id>/``. This watcher keeps a cached
view and can force a synchronous rescan before quiescence decisions.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from watchfiles import awatch  # pyright: ignore[reportUnknownVariableType]

from meridian.lib.core.spawn_lifecycle import TERMINAL_SPAWN_STATUSES
from meridian.lib.core.types import SpawnId


class PiDiskWatcher:
    """Async disk-state observer for one spawned Pi session."""

    def __init__(self, runtime_root: Path, current_spawn_id: SpawnId) -> None:
        self._runtime_root = runtime_root
        self._current_spawn_id = str(current_spawn_id)
        self._spawns_dir = runtime_root / "spawns"
        self._bash_dir = runtime_root / "pi-bash" / self._current_spawn_id
        self._bash_records_path = self._bash_dir / "bash-records.json"
        self._notification_marker_path = self._bash_dir / "last-notification.json"
        self._pending_child_spawns = False
        self._tracked_bash_bg = False
        self._last_notification_ts: float | None = None
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self._spawns_dir.mkdir(parents=True, exist_ok=True)
        self._bash_dir.mkdir(parents=True, exist_ok=True)
        await self.force_rescan()
        self._tasks = [
            asyncio.create_task(self._watch_dir(self._spawns_dir)),
            asyncio.create_task(self._watch_dir(self._bash_dir)),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    async def force_rescan(self) -> None:
        self._pending_child_spawns = self._scan_pending_child_spawns()
        self._tracked_bash_bg = self._scan_tracked_bash_bg()
        self._last_notification_ts = self._read_last_notification_ts()

    def has_pending_child_spawns(self) -> bool:
        return self._pending_child_spawns

    def has_tracked_bash_bg(self) -> bool:
        return self._tracked_bash_bg

    def last_notification_ts(self) -> float | None:
        return self._last_notification_ts

    async def _watch_dir(self, directory: Path) -> None:
        async for _changes in awatch(directory):
            await self.force_rescan()

    def _scan_pending_child_spawns(self) -> bool:
        if not self._spawns_dir.is_dir():
            return False
        for child in self._spawns_dir.iterdir():
            state_path = child / "state.json"
            if not state_path.is_file():
                continue
            data = _read_json_object(state_path)
            if data.get("parent_id") != self._current_spawn_id:
                continue
            status = data.get("status")
            if not isinstance(status, str) or status not in TERMINAL_SPAWN_STATUSES:
                return True
        return False

    def _scan_tracked_bash_bg(self) -> bool:
        data = _read_json_object(self._bash_records_path)
        records_raw = data.get("records")
        if not isinstance(records_raw, dict):
            return False
        records = cast("dict[str, object]", records_raw)
        for raw in records.values():
            if not isinstance(raw, dict):
                continue
            record = cast("dict[str, object]", raw)
            if (
                record.get("is_tracked") is True
                and record.get("is_background") is True
                and record.get("status") == "running"
            ):
                return True
        return False

    def _read_last_notification_ts(self) -> float | None:
        data = _read_json_object(self._notification_marker_path)
        raw = data.get("ts_epoch_secs")
        if isinstance(raw, int | float):
            return float(raw)
        return None


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return cast("dict[str, Any]", data) if isinstance(data, dict) else {}
