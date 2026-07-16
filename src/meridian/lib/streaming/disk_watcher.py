"""Disk-state observer for Pi quiescence.

Pi background-work coordination is file-backed: spawn records under ``spawns/`` and
managed bash records under ``pi-bash/<spawn-id>/``. This watcher keeps a cached
view and can force a synchronous rescan before quiescence decisions.

Child discovery is O(children), not O(total spawns). Directory events discover
new children; a bounded poll observes known child terminal transitions and
new child directories whose ``state.json`` was written after the directory.
No per-child inotify watchers are created.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from watchfiles import awatch  # pyright: ignore[reportUnknownVariableType]

from meridian.lib.core.spawn_lifecycle import TERMINAL_SPAWN_STATUSES
from meridian.lib.core.types import SpawnId
from meridian.lib.state import spawn_store
from meridian.lib.streaming.completion_contracts import EvidenceFailure
from meridian.lib.streaming.pi_work_ledger import PiPrivateWorkLedger

# Bounded poll while known or unresolved children may still be pending. ``awatch``
# on ``spawns/`` is non-recursive, so ``spawns/<child>/state.json`` updates do
# not wake the watcher; polling observes terminal transitions and late state
# writes without per-child tasks.
_PENDING_DISK_POLL_INTERVAL_SECONDS = 0.25

# Spawn allocation creates the directory immediately before its atomic state write.
# A newer numeric ID is causal evidence that an unresolved directory could be a
# child whose state write is still in flight. Bound that uncertainty using the
# process monotonic clock so a crashed allocation cannot remain pending forever.
_UNRESOLVED_CHILD_EXPIRY_SECONDS = 30.0

_QuiescenceDiskSnapshot = tuple[int, int, bool, bool, float | None]


@dataclass(frozen=True, slots=True)
class _ConfirmedChild:
    pass


@dataclass(frozen=True, slots=True)
class _UnresolvedChild:
    pass


@dataclass(frozen=True, slots=True)
class _RejectedChild:
    """Tombstone: directory was considered and rejected (expired or wrong parent).

    Prevents re-admission by _discover_child_spawns on subsequent scans.
    Not counted as pending; not checked against disk.
    """

    pass


_CONFIRMED_CHILD = _ConfirmedChild()
_UNRESOLVED_CHILD = _UnresolvedChild()
_REJECTED_CHILD = _RejectedChild()
_TrackedChild = _ConfirmedChild | _UnresolvedChild | _RejectedChild


class PiDiskWatcher:
    """Async disk-state observer for one spawned Pi session."""

    def __init__(
        self,
        runtime_root: Path,
        current_spawn_id: SpawnId,
        ledger: PiPrivateWorkLedger | None = None,
    ) -> None:
        self._runtime_root = runtime_root
        self._current_spawn_id = str(current_spawn_id)
        self._spawns_dir = runtime_root / "spawns"
        self._bash_dir = runtime_root / "pi-bash" / self._current_spawn_id
        self._bash_records_path = self._bash_dir / "bash-records.json"
        self._notification_marker_path = self._bash_dir / "last-notification.json"
        self._pending_child_spawns = False
        self._pending_child_spawn_count = 0
        self._pending_confirmed_child_ids: tuple[str, ...] = ()
        self._ledger = ledger or PiPrivateWorkLedger()
        self._tasks: list[asyncio.Task[None]] = []
        # Keeping confirmed children and unresolved candidates in one typed
        # collection prevents candidate membership and expiry from drifting.
        self._child_spawns: dict[str, _TrackedChild] = {}
        self._state_changed = asyncio.Event()
        self._change_generation = 0
        self._delivered_change_generation = 0

    async def start(self) -> None:
        self._spawns_dir.mkdir(parents=True, exist_ok=True)
        self._bash_dir.mkdir(parents=True, exist_ok=True)
        await self.force_rescan()
        self._delivered_change_generation = self._change_generation
        self._state_changed = asyncio.Event()
        self._tasks = [
            asyncio.create_task(self._watch_spawns_dir()),
            asyncio.create_task(self._watch_bash_dir()),
        ]

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
        self._refresh_cached_state(discover=True)

    async def wait_for_change(self) -> None:
        """Wait until disk-backed quiescence inputs change."""
        while True:
            if self._change_generation > self._delivered_change_generation:
                self._delivered_change_generation = self._change_generation
                return
            event = self._state_changed
            poll_interval = (
                _PENDING_DISK_POLL_INTERVAL_SECONDS
                if self._pending_child_spawns or self._has_unresolved_candidates()
                else None
            )
            if poll_interval is not None:
                try:
                    await asyncio.wait_for(event.wait(), timeout=poll_interval)
                except TimeoutError:
                    self._refresh_cached_state(discover=False)
                    continue
            else:
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
            self._pending_child_spawn_count,
            self._unresolved_candidate_count(),
            self._pending_child_spawns,
            self._ledger.tracked_bash_bg(),
            self._ledger.last_notification_ts(),
        )

    def _refresh_cached_state(self, *, discover: bool = False) -> bool:
        prior = self._quiescence_disk_snapshot()
        if discover:
            self._discover_child_spawns()
        self._resolve_candidate_child_spawns()
        self._pending_child_spawn_count = self._scan_pending_child_spawn_count()
        self._pending_child_spawns = self._pending_child_spawn_count > 0
        self._ledger.update_disk_evidence(
            tracked_bash_bg=self._scan_tracked_bash_bg(),
            last_notification_ts=self._read_last_notification_ts(),
        )
        changed = self._quiescence_disk_snapshot() != prior
        if changed:
            self._change_generation += 1
            self._state_changed.set()
        return changed

    def has_pending_child_spawns(self) -> bool:
        return self._pending_child_spawns

    def pending_child_spawn_count(self) -> int:
        return self._pending_child_spawn_count

    def pending_confirmed_child_ids(self) -> tuple[str, ...]:
        return self._pending_confirmed_child_ids

    def unresolved_child_ids(self) -> tuple[str, ...]:
        return self._ledger.allocation_uncertainty_ids()

    def has_tracked_bash_bg(self) -> bool:
        return self._ledger.tracked_bash_bg()

    def last_notification_ts(self) -> float | None:
        return self._ledger.last_notification_ts()

    def evidence_failure(self) -> EvidenceFailure | None:
        return self._ledger.evidence_failure()

    async def _watch_spawns_dir(self) -> None:
        """Watch for new child directories under spawns/.

        Directory events discover child candidates. Known-child ``state.json``
        updates are observed by bounded polling because the watch is
        non-recursive.
        """
        async for changes in awatch(self._spawns_dir, recursive=False):
            found_new = False
            for _change, raw_path in changes:
                path = Path(str(raw_path))
                if (
                    path.parent == self._spawns_dir
                    and path.name.startswith("p")
                    and path.name != self._current_spawn_id
                    and path.name not in self._child_spawns
                    and path.is_dir()
                ):
                    # If state.json isn't written yet, force_rescan() at next idle catches it.
                    state_path = path / "state.json"
                    data = self._read_json_object(state_path)
                    if data is None:
                        continue
                    if data.get("parent_id") == self._current_spawn_id:
                        self._child_spawns[path.name] = _CONFIRMED_CHILD
                        found_new = True
                    elif (
                        spawn_store.is_spawn_id_shape(path.name)
                        and (not state_path.exists() or not data)
                        and self._is_newer_numeric_spawn_id(path.name)
                    ):
                        self._admit_unresolved_candidate(path.name)
                        found_new = True
            if found_new:
                self._refresh_cached_state()

    async def _watch_bash_dir(self) -> None:
        async for _changes in awatch(self._bash_dir, recursive=False):
            self._refresh_cached_state()

    def _discover_child_spawns(self) -> None:
        """Scan spawns/ for children not yet in _child_spawns.

        O(N) on first run, near-free after (skips known children).
        Also called on every force_rescan() to catch directories missed
        by the inotify watcher (e.g. state.json not yet written).
        """
        if not self._spawns_dir.exists():
            return
        try:
            children = tuple(self._spawns_dir.iterdir())
        except OSError as exc:
            self._record_read_failure(self._spawns_dir, str(exc))
            return
        self._clear_read_failure(self._spawns_dir)
        for child in children:
            if not child.is_dir():
                continue
            if child.name == self._current_spawn_id:
                continue
            if child.name in self._child_spawns:
                continue
            state_path = child / "state.json"
            data = self._read_json_object(state_path)
            if data is None:
                continue
            parent_id = data.get("parent_id")
            if parent_id == self._current_spawn_id:
                self._child_spawns[child.name] = _CONFIRMED_CHILD
            elif (
                spawn_store.is_spawn_id_shape(child.name)
                and (not state_path.exists() or not data)
                and self._is_newer_numeric_spawn_id(child.name)
            ):
                self._admit_unresolved_candidate(child.name)

    def _resolve_candidate_child_spawns(self) -> None:
        now_monotonic = time.monotonic()
        for spawn_id, child in list(self._child_spawns.items()):
            if not isinstance(child, _UnresolvedChild):
                continue
            deadline_monotonic = self._ledger.allocation_deadline(spawn_id)
            if deadline_monotonic is None:
                self._child_spawns[spawn_id] = _REJECTED_CHILD
                continue
            if now_monotonic >= deadline_monotonic:
                self._child_spawns[spawn_id] = _REJECTED_CHILD
                self._ledger.resolve_allocation_uncertainty(spawn_id)
                self._clear_read_failure(self._spawns_dir / spawn_id / "state.json")
                continue
            state_path = self._spawns_dir / spawn_id / "state.json"
            data = self._read_json_object(state_path)
            if data is None:
                continue
            parent_id = data.get("parent_id")
            if parent_id == self._current_spawn_id:
                self._child_spawns[spawn_id] = _CONFIRMED_CHILD
                self._ledger.resolve_allocation_uncertainty(spawn_id)
            elif not state_path.parent.exists() or (state_path.exists() and data):
                self._child_spawns[spawn_id] = _REJECTED_CHILD
                self._ledger.resolve_allocation_uncertainty(spawn_id)

    def _scan_pending_child_spawn_count(self) -> int:
        count = 0
        confirmed_ids: list[str] = []
        for spawn_id, child in list(self._child_spawns.items()):
            if isinstance(child, _RejectedChild):
                continue
            if isinstance(child, _UnresolvedChild):
                # Expired candidates are already tombstoned by
                # _resolve_candidate_child_spawns before this method runs.
                count += 1
                continue
            state_path = self._spawns_dir / spawn_id / "state.json"
            data = self._read_json_object(state_path)
            if data is None:
                count += 1
                confirmed_ids.append(spawn_id)
                continue
            if data.get("parent_id") != self._current_spawn_id:
                self._child_spawns[spawn_id] = _REJECTED_CHILD
                continue
            status = data.get("status")
            if not isinstance(status, str) or status not in TERMINAL_SPAWN_STATUSES:
                count += 1
                confirmed_ids.append(spawn_id)
        self._pending_confirmed_child_ids = tuple(sorted(confirmed_ids))
        return count

    def _is_newer_numeric_spawn_id(self, spawn_id: str) -> bool:
        if not spawn_store.is_spawn_id_shape(self._current_spawn_id):
            return False
        return int(spawn_id[1:]) > int(self._current_spawn_id[1:])

    def _admit_unresolved_candidate(self, spawn_id: str) -> None:
        if spawn_id in self._child_spawns:
            return
        self._child_spawns[spawn_id] = _UNRESOLVED_CHILD
        self._ledger.admit_allocation_uncertainty(
            spawn_id,
            deadline_monotonic=time.monotonic() + _UNRESOLVED_CHILD_EXPIRY_SECONDS,
        )

    def _has_unresolved_candidates(self) -> bool:
        return self._ledger.has_allocation_uncertainty()

    def _unresolved_candidate_count(self) -> int:
        return len(self._ledger.allocation_uncertainty_ids())

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
