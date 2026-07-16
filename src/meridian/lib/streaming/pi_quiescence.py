"""Pi-specific quiescence state for streaming drain.

The generic drain loop owns event persistence and subscriber fan-out. This module
owns the Pi policy that also depends on disk-backed background work state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from meridian.lib.core.types import SpawnId
from meridian.lib.streaming.completion_contracts import EvidenceFailure
from meridian.lib.streaming.disk_watcher import PiDiskWatcher


@dataclass
class PiQuiescenceTracker:
    """Track Pi parent-idle and disk-backed background work quiescence."""

    runtime_root: Path
    spawn_id: SpawnId
    enabled: bool
    _parent_idle: bool = False
    _parent_idle_epoch: float | None = None
    _disk_watcher: PiDiskWatcher | None = None

    @classmethod
    def for_connection(
        cls,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        is_pi_connection: bool,
        session_role: str,
    ) -> PiQuiescenceTracker:
        enabled = is_pi_connection and session_role == "spawned"
        return cls(runtime_root=runtime_root, spawn_id=spawn_id, enabled=enabled)

    @property
    def parent_idle(self) -> bool:
        return self._parent_idle

    async def start(self) -> None:
        if not self.enabled:
            return
        self._disk_watcher = PiDiskWatcher(self.runtime_root, self.spawn_id)
        await self._disk_watcher.start()

    async def stop(self) -> None:
        if self._disk_watcher is not None:
            await self._disk_watcher.stop()
        self._disk_watcher = None

    def mark_turn_active(self) -> None:
        self._parent_idle = False
        self._parent_idle_epoch = None

    async def mark_idle(self) -> None:
        self._parent_idle = True
        self._parent_idle_epoch = time.time()
        await self.refresh_disk_state()

    async def refresh_disk_state(self) -> None:
        if self._disk_watcher is not None:
            await self._disk_watcher.force_rescan()

    async def wait_for_disk_change(self) -> None:
        if self._disk_watcher is not None:
            await self._disk_watcher.wait_for_change()

    def has_pending_child_spawns(self) -> bool:
        return bool(self._disk_watcher and self._disk_watcher.has_pending_child_spawns())

    def has_tracked_bash_bg(self) -> bool:
        return bool(self._disk_watcher and self._disk_watcher.has_tracked_bash_bg())

    def pending_child_spawn_count(self) -> int:
        if self._disk_watcher is None:
            return 0
        return self._disk_watcher.pending_child_spawn_count()

    def pending_confirmed_child_ids(self) -> tuple[str, ...]:
        if self._disk_watcher is None:
            return ()
        return self._disk_watcher.pending_confirmed_child_ids()

    def unresolved_child_ids(self) -> tuple[str, ...]:
        if self._disk_watcher is None:
            return ()
        return self._disk_watcher.unresolved_child_ids()

    def has_pending_disk_notification(self) -> bool:
        return not self._no_pending_disk_notifications()

    def evidence_failure(self) -> EvidenceFailure | None:
        if self._disk_watcher is None:
            return None
        return self._disk_watcher.evidence_failure()

    def is_quiescent(self) -> bool:
        if not self.enabled or not self._parent_idle:
            return False
        if self._disk_watcher is None:
            return True
        return (
            not self._disk_watcher.has_pending_child_spawns()
            and not self._disk_watcher.has_tracked_bash_bg()
            and not self.has_pending_disk_notification()
        )

    def _no_pending_disk_notifications(self) -> bool:
        if self._disk_watcher is None or self._parent_idle_epoch is None:
            return True
        last_notification_ts = self._disk_watcher.last_notification_ts()
        return last_notification_ts is None or self._parent_idle_epoch > last_notification_ts
