"""Pi-specific quiescence state for streaming drain.

The generic drain loop owns event persistence and subscriber fan-out. This module
owns the Pi policy that also depends on disk-backed background work state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from meridian.lib.core.types import SpawnId
from meridian.lib.streaming.completion_contracts import EvidenceFailure
from meridian.lib.streaming.disk_watcher import PiDiskWatcher
from meridian.lib.streaming.pi_work_ledger import (
    PiPrivateWorkLedger,
    PiPrivateWorkSnapshot,
)


@dataclass
class PiQuiescenceTracker:
    """Track Pi parent-idle and disk-backed background work quiescence."""

    runtime_root: Path
    spawn_id: SpawnId
    enabled: bool
    _ledger: PiPrivateWorkLedger = field(default_factory=PiPrivateWorkLedger)
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
        ledger: PiPrivateWorkLedger | None = None,
    ) -> PiQuiescenceTracker:
        enabled = is_pi_connection and session_role == "spawned"
        return cls(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            enabled=enabled,
            _ledger=ledger or PiPrivateWorkLedger(),
        )

    @property
    def parent_idle(self) -> bool:
        return self._parent_idle

    async def start(self) -> None:
        if not self.enabled:
            return
        self._disk_watcher = PiDiskWatcher(
            self.runtime_root,
            self.spawn_id,
            self._ledger,
        )
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

    def has_tracked_bash_bg(self) -> bool:
        return self._ledger.tracked_bash_bg()

    def has_pending_disk_notification(self) -> bool:
        return not self._no_pending_disk_notifications()

    def evidence_failure(self) -> EvidenceFailure | None:
        return self._ledger.evidence_failure()

    def private_work_snapshot(self) -> PiPrivateWorkSnapshot:
        return self._ledger.blocker_snapshot(parent_idle_epoch=self._parent_idle_epoch)

    def _no_pending_disk_notifications(self) -> bool:
        return not self.private_work_snapshot().pending_disk_notification
