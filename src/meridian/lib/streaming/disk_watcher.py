"""Observe validated Pi private files and causal result receipts."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel
from watchfiles import awatch  # pyright: ignore[reportUnknownVariableType]

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.pi_private_state import (
    BashEvidenceFile,
    ClearedSpawns,
    DeliveryFault,
    DeliveryObservations,
    DeliveryReceipts,
    SpawnObservations,
)
from meridian.lib.streaming.completion_contracts import EvidenceFailure
from meridian.lib.streaming.pi_work_ledger import PiPrivateWorkLedger

_Model = TypeVar("_Model", bound=BaseModel)


class PiDiskWatcher:
    def __init__(
        self,
        runtime_root: Path,
        current_spawn_id: SpawnId,
        ledger: PiPrivateWorkLedger | None = None,
    ) -> None:
        self._spawn_id = str(current_spawn_id)
        self._bash_dir = runtime_root / "pi-bash" / self._spawn_id
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
        self._tasks = [asyncio.create_task(self._watch_bash_dir())]

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
        bash = self._read("bash-records.json", BashEvidenceFile)
        receipts = self._read("delivery-receipts.json", DeliveryReceipts)
        observed = self._read("delivery-observations.json", DeliveryObservations)
        explicit = self._read("observed-spawns.json", SpawnObservations)
        cleared = self._read("cleared-spawns.json", ClearedSpawns)
        fault = self._read("delivery-fault.json", DeliveryFault)
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

    async def _watch_bash_dir(self) -> None:
        try:
            async for _changes in awatch(
                self._bash_dir,
                recursive=False,
                stop_event=self._stop_event,
                rust_timeout=100,
            ):
                self._refresh_cached_state()
        except Exception as exc:
            self._ledger.record_read_failure(self._bash_dir, str(exc))
            self._change_generation += 1
            self._state_changed.set()

    def _read(self, name: str, model: type[_Model], *, scoped: bool = True) -> _Model | None:
        file = self._bash_dir / name
        try:
            value = model.model_validate_json(file.read_text(encoding="utf-8"))
            if scoped and getattr(value, "spawn_id", None) != self._spawn_id:
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
