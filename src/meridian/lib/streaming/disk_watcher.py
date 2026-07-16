"""Disk-state observer for Pi quiescence.

Pi private-work coordination is file-backed under ``pi-bash/<spawn-id>/``.
This watcher keeps a cached view and can force a synchronous rescan before
quiescence decisions.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from watchfiles import awatch  # pyright: ignore[reportUnknownVariableType]

from meridian.lib.core.types import SpawnId
from meridian.lib.streaming.completion_contracts import EvidenceFailure
from meridian.lib.streaming.pi_work_ledger import PiPrivateWorkLedger

_QuiescenceDiskSnapshot = tuple[bool, float | None]


class PiDiskWatcher:
    """Async disk-state observer for one spawned Pi session."""

    def __init__(
        self,
        runtime_root: Path,
        current_spawn_id: SpawnId,
        ledger: PiPrivateWorkLedger | None = None,
    ) -> None:
        self._current_spawn_id = str(current_spawn_id)
        self._bash_dir = runtime_root / "pi-bash" / self._current_spawn_id
        self._bash_records_path = self._bash_dir / "bash-records.json"
        self._notification_marker_path = self._bash_dir / "last-notification.json"
        self._ledger = ledger or PiPrivateWorkLedger()
        self._tasks: list[asyncio.Task[None]] = []
        self._state_changed = asyncio.Event()
        self._change_generation = 0
        self._delivered_change_generation = 0

    async def start(self) -> None:
        self._bash_dir.mkdir(parents=True, exist_ok=True)
        await self.force_rescan()
        self._delivered_change_generation = self._change_generation
        self._state_changed = asyncio.Event()
        self._tasks = [asyncio.create_task(self._watch_bash_dir())]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            done, pending = await asyncio.wait(self._tasks, timeout=0.2)
            for task in done:
                with suppress(asyncio.CancelledError, Exception):
                    task.result()
            for task in pending:
                task.add_done_callback(_consume_task_result)
        self._tasks = []

    async def force_rescan(self) -> None:
        self._refresh_cached_state()

    async def wait_for_change(self) -> None:
        """Wait until disk-backed quiescence inputs change."""
        while True:
            if self._change_generation > self._delivered_change_generation:
                self._delivered_change_generation = self._change_generation
                return
            event = self._state_changed
            await event.wait()

            if self._change_generation > self._delivered_change_generation:
                self._delivered_change_generation = self._change_generation
                return
            if event is self._state_changed:
                # Replace the stale-set Event. asyncio.Event.set() is sticky, so a
                # previously-delivered generation leaves the event object set and
                # event.wait() returns immediately on the next call. Replacing it
                # here prevents a false wakeup on the next loop iteration.
                self._state_changed = asyncio.Event()

    def _quiescence_disk_snapshot(self) -> _QuiescenceDiskSnapshot:
        return (
            self._ledger.tracked_bash_bg(),
            self._ledger.last_notification_ts(),
        )

    def _refresh_cached_state(self) -> bool:
        prior = self._quiescence_disk_snapshot()
        self._ledger.update_disk_evidence(
            tracked_bash_bg=self._scan_tracked_bash_bg(),
            last_notification_ts=self._read_last_notification_ts(),
        )
        changed = self._quiescence_disk_snapshot() != prior
        if changed:
            self._change_generation += 1
            self._state_changed.set()
        return changed

    def has_tracked_bash_bg(self) -> bool:
        return self._ledger.tracked_bash_bg()

    def last_notification_ts(self) -> float | None:
        return self._ledger.last_notification_ts()

    def evidence_failure(self) -> EvidenceFailure | None:
        return self._ledger.evidence_failure()

    async def _watch_bash_dir(self) -> None:
        async for _changes in awatch(self._bash_dir, recursive=False):
            self._refresh_cached_state()

    def _scan_tracked_bash_bg(self) -> bool:
        data = self._read_json_object(self._bash_records_path)
        if data is None:
            return False
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
        data = self._read_json_object(self._notification_marker_path)
        if data is None:
            return None
        raw = data.get("ts_epoch_secs")
        if isinstance(raw, int | float):
            return float(raw)
        return None

    def _read_json_object(self, path: Path) -> dict[str, Any] | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._clear_read_failure(path)
            return {}
        except OSError as exc:
            self._record_read_failure(path, str(exc))
            return None
        except UnicodeError as exc:
            self._record_read_failure(path, str(exc))
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self._record_read_failure(path, "invalid JSON")
            return None
        if not isinstance(data, dict):
            self._record_read_failure(path, "expected JSON object")
            return None
        self._clear_read_failure(path)
        return cast("dict[str, Any]", data)

    def _record_read_failure(self, path: Path, detail: str) -> None:
        if not self._ledger.record_read_failure(path, detail):
            return
        self._change_generation += 1
        self._state_changed.set()

    def _clear_read_failure(self, path: Path) -> None:
        if not self._ledger.clear_read_failure(path):
            return
        self._change_generation += 1
        self._state_changed.set()


def _consume_task_result(task: asyncio.Task[None]) -> None:
    with suppress(asyncio.CancelledError, Exception):
        task.result()
