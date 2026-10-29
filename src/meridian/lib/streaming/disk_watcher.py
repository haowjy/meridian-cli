"""Observe validated Pi private files and causal result receipts."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel
from watchfiles import (
    Change,
    awatch,  # pyright: ignore[reportUnknownVariableType]  # watchfiles exposes an incompletely typed async generator.
)

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.pi_private_state import (
    CLEARED_LAUNCHES_FILENAME,
    DELIVERY_RECEIPTS_FILENAME,
    LEGACY_CLEARED_SPAWNS_FILENAME,
    LEGACY_DELIVERY_RECEIPTS_FILENAME,
    LEGACY_OBSERVED_SPAWNS_FILENAME,
    OBSERVED_LAUNCHES_FILENAME,
    BashEvidenceFile,
    DeliveryFault,
    DeliveryObservations,
    pi_coordination_dir,
    read_pi_consumption_state,
    read_pi_private_doc,
)
from meridian.lib.state.paths import observed_launches_path
from meridian.lib.streaming.completion_contracts import EvidenceFailure
from meridian.lib.streaming.pi_work_ledger import PiPrivateWorkLedger

_Model = TypeVar("_Model", bound=BaseModel)

# Everything `_refresh_cached_state` reads. The parent's spawn directory also holds
# unrelated, frequently rewritten files, so only these names may wake the watcher.
_WATCHED_NAMES = frozenset(
    {
        "bash-records.json",
        DELIVERY_RECEIPTS_FILENAME,
        "delivery-observations.json",
        OBSERVED_LAUNCHES_FILENAME,
        CLEARED_LAUNCHES_FILENAME,
        # Older builds' files, still read until the ledgers they key are retired.
        LEGACY_DELIVERY_RECEIPTS_FILENAME,
        LEGACY_OBSERVED_SPAWNS_FILENAME,
        LEGACY_CLEARED_SPAWNS_FILENAME,
        "delivery-fault.json",
    }
)


def _is_watched_file(_change: Change, path: str) -> bool:
    return os.path.basename(path) in _WATCHED_NAMES


class PiDiskWatcher:
    def __init__(
        self,
        runtime_root: Path,
        current_spawn_id: SpawnId,
        ledger: PiPrivateWorkLedger | None = None,
    ) -> None:
        self._spawn_id = str(current_spawn_id)
        self._runtime_root = runtime_root
        self._bash_dir = pi_coordination_dir(runtime_root, self._spawn_id)
        self._observations_path = observed_launches_path(runtime_root, self._spawn_id)
        self._ledger = ledger or PiPrivateWorkLedger()
        self._tasks: list[asyncio.Task[None]] = []
        self._state_changed = asyncio.Event()
        self._change_generation = 0
        self._delivered_change_generation = 0
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        self._bash_dir.mkdir(parents=True, exist_ok=True)
        await self.force_rescan()
        self._delivered_change_generation = self._change_generation
        self._state_changed = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._tasks = [asyncio.create_task(self._watch_state_dirs())]

    async def stop(self) -> None:
        self._stop_event.set()
        if self._tasks:
            done, pending = await asyncio.wait(self._tasks, timeout=0.2)
            for task in done:
                _consume_task_result(task)
            for task in pending:
                task.cancel()
                task.add_done_callback(_consume_task_result)
        self._tasks = []

    async def force_rescan(self) -> None:
        self._refresh_cached_state()

    async def wait_for_change(self) -> None:
        while True:
            if self._change_generation > self._delivered_change_generation:
                self._delivered_change_generation = self._change_generation
                return
            event = self._state_changed
            await event.wait()
            if event is self._state_changed:
                self._state_changed = asyncio.Event()

    def _refresh_cached_state(self) -> bool:
        prior = self._ledger.blocker_snapshot()
        bash = self._read(self._bash_dir / "bash-records.json", BashEvidenceFile)
        observed = self._read(self._bash_dir / "delivery-observations.json", DeliveryObservations)
        consumption = read_pi_consumption_state(
            self._runtime_root,
            self._spawn_id,
            on_read=self._record_consumption_read,
        )
        fault = self._read(self._bash_dir / "delivery-fault.json", DeliveryFault)
        if fault is not None and fault.error:
            self._ledger.record_read_failure(self._bash_dir / "delivery-fault.json", fault.error)
        if bash is not None:
            if any(key != record.bash_id for key, record in bash.records.items()):
                self._ledger.record_read_failure(
                    self._bash_dir / "bash-records.json", "record identity mismatch"
                )
            elif bash.runtime_error:
                self._ledger.record_read_failure(
                    self._bash_dir / "bash-records.json", bash.runtime_error
                )
        unobserved = set(consumption.receipt_message_ids) - set(
            observed.observed_message_ids if observed else ()
        )
        self._ledger.update_disk_evidence(
            bash=tuple(bash.records.values()) if bash else (),
            consumed=consumption.consumed_ids,
            unobserved_messages=frozenset(unobserved),
        )
        changed = self._ledger.blocker_snapshot() != prior
        if changed:
            self._change_generation += 1
            self._state_changed.set()
        return changed

    def has_tracked_bash_bg(self) -> bool:
        return self._ledger.tracked_bash_bg()

    def evidence_failure(self) -> EvidenceFailure | None:
        return self._ledger.evidence_failure()

    async def _watch_state_dirs(self) -> None:
        # Pi coordination files keep the bash-directory watch. The spawn-directory
        # watch adds the observations file without waking on unrelated files.
        watched = [self._bash_dir]
        if self._observations_path.parent.is_dir():
            watched.append(self._observations_path.parent)
        try:
            async for _changes in awatch(
                *watched,
                watch_filter=_is_watched_file,
                recursive=False,
                stop_event=self._stop_event,
                rust_timeout=100,
            ):
                self._refresh_cached_state()
        except Exception as exc:
            self._ledger.record_read_failure(self._bash_dir, str(exc))
            self._change_generation += 1
            self._state_changed.set()

    def _read(self, file: Path, model: type[_Model]) -> _Model | None:
        try:
            value = read_pi_private_doc(file, model, spawn_id=self._spawn_id)
        except (OSError, UnicodeError, ValueError) as exc:
            self._ledger.record_read_failure(file, str(exc))
            return None
        if value is None:
            self._ledger.clear_read_failure(file)
            return None
        self._ledger.clear_read_failure(file)
        return value

    def _record_consumption_read(self, file: Path, error: str | None) -> None:
        if error is None:
            self._ledger.clear_read_failure(file)
        else:
            self._ledger.record_read_failure(file, error)


def _consume_task_result(task: asyncio.Task[None]) -> None:
    with suppress(asyncio.CancelledError, Exception):
        task.result()
