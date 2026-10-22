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
    BashEvidenceFile,
    ClearedSpawns,
    DeliveryFault,
    DeliveryObservations,
    DeliveryReceipts,
)
from meridian.lib.state.paths import observed_spawns_path
from meridian.lib.state.spawn_observations import SpawnObservations, read_legacy
from meridian.lib.streaming.completion_contracts import EvidenceFailure
from meridian.lib.streaming.pi_work_ledger import PiPrivateWorkLedger

_Model = TypeVar("_Model", bound=BaseModel)

# Everything `_refresh_cached_state` reads. The parent's spawn directory also holds
# unrelated, frequently rewritten files, so only these names may wake the watcher.
_WATCHED_NAMES = frozenset(
    {
        "bash-records.json",
        "delivery-receipts.json",
        "delivery-observations.json",
        "observed-spawns.json",
        "cleared-spawns.json",
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
        self._bash_dir = runtime_root / "pi-bash" / self._spawn_id
        self._observations_path = observed_spawns_path(runtime_root, self._spawn_id)
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
        receipts = self._read(self._bash_dir / "delivery-receipts.json", DeliveryReceipts)
        observed = self._read(self._bash_dir / "delivery-observations.json", DeliveryObservations)
        explicit = self._read(self._observations_path, SpawnObservations)
        legacy_explicit = read_legacy(self._runtime_root, self._spawn_id)
        cleared = self._read(self._bash_dir / "cleared-spawns.json", ClearedSpawns)
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
        consumed: set[str] = (
            {work_id for ids in receipts.messages.values() for work_id in ids}
            if receipts
            else set()
        )
        consumed.update(explicit.observed_spawn_ids if explicit else ())
        consumed.update(legacy_explicit.observed_spawn_ids if legacy_explicit else ())
        consumed.update(cleared.cleared_spawn_ids if cleared else ())
        unobserved = set(receipts.messages if receipts else ()) - set(
            observed.observed_message_ids if observed else ()
        )
        self._ledger.update_disk_evidence(
            bash=tuple(bash.records.values()) if bash else (),
            consumed=frozenset(consumed),
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
        # The spawn directory holds the current observed-spawns.json; the bash
        # directory still holds the legacy copy a pre-move `spawn wait` may write.
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
            value = model.model_validate_json(file.read_text(encoding="utf-8"))
            if getattr(value, "spawn_id", None) != self._spawn_id:
                raise ValueError("private-file parent identity mismatch")
        except FileNotFoundError:
            self._ledger.clear_read_failure(file)
            return None
        except (OSError, UnicodeError, ValueError) as exc:
            self._ledger.record_read_failure(file, str(exc))
            return None
        self._ledger.clear_read_failure(file)
        return value


def _consume_task_result(task: asyncio.Task[None]) -> None:
    with suppress(asyncio.CancelledError, Exception):
        task.result()
