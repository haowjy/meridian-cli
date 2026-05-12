"""Shared managed-primary runtime helpers."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import psutil

from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.state.liveness import is_process_alive
from meridian.lib.state.primary_meta import PrimaryMetadata, read_primary_metadata
from meridian.lib.state.spawn.model import SpawnRecord

if TYPE_CHECKING:
    from meridian.lib.state.reaper import ArtifactSnapshot, ReconciliationDecision


NESTED_SCOPE_KEYS = ("item", "request", "turn", "params", "data", "message", "payload")


@dataclass(frozen=True)
class ManagedPrimarySnapshot:
    """Snapshot of managed primary runtime state for reconciliation."""

    metadata: PrimaryMetadata
    launcher_pid_alive: bool
    started_epoch: float | None


@dataclass(frozen=True)
class ReconciliationContext:
    """Context passed to reconciliation strategies."""

    record: SpawnRecord
    artifact_snapshot: ArtifactSnapshot
    managed_snapshot: ManagedPrimarySnapshot
    now: float


@dataclass(frozen=True)
class CausalEnvelopeFields:
    """Per-event causal metadata persisted to managed-primary history."""

    turn_id: str | None
    item_id: str | None
    request_id: str | None
    interrupt_epoch: int
    stale_after_interrupt: bool


class ManagedPrimaryCausalTracker:
    """Derive causal metadata for append-only managed-primary history events."""

    _TURN_ID_KEYS = ("turn_id", "turnId", "turnID")
    _ITEM_ID_KEYS = ("item_id", "itemId", "call_id", "callId", "callID", "tool_use_id")
    _REQUEST_ID_KEYS = ("request_id", "requestId")
    _INTERRUPT_EPOCH_KEYS = ("interrupt_epoch", "interruptEpoch")
    _STALE_KEYS = ("stale_after_interrupt", "staleAfterInterrupt")

    def __init__(self) -> None:
        self._active_turn_id: str | None = None
        self._interrupt_epoch = 0
        self._interrupted_turn_ids: set[str] = set()
        self._superseded_turn_ids: set[str] = set()
        self._item_to_turn: dict[str, str] = {}
        self._request_to_turn: dict[str, str] = {}

    def derive(self, event: HarnessEvent) -> CausalEnvelopeFields:
        """Compute causal envelope fields and update correlation state."""

        normalized_type = _normalize_event_type(event.event_type)
        payload = event.payload

        turn_id = self._extract_turn_id(normalized_type, payload)
        item_id = self._extract_item_id(normalized_type, payload)
        request_id = self._extract_request_id(normalized_type, payload)

        if turn_id is None and item_id is not None:
            turn_id = self._item_to_turn.get(item_id)
        if turn_id is None and request_id is not None:
            turn_id = self._request_to_turn.get(request_id)

        explicit_interrupt_epoch = _extract_int(payload, self._INTERRUPT_EPOCH_KEYS)
        if explicit_interrupt_epoch is not None:
            self._interrupt_epoch = max(self._interrupt_epoch, explicit_interrupt_epoch)

        if _is_interrupt_request_event(normalized_type, payload):
            if explicit_interrupt_epoch is None:
                self._interrupt_epoch += 1
            interrupted_turn = turn_id or self._active_turn_id
            if interrupted_turn is not None:
                self._interrupted_turn_ids.add(interrupted_turn)
                turn_id = interrupted_turn

        if _is_turn_started_event(normalized_type, payload) and turn_id is not None:
            if self._interrupted_turn_ids and turn_id not in self._interrupted_turn_ids:
                self._superseded_turn_ids.update(self._interrupted_turn_ids)
            self._active_turn_id = turn_id

        if (
            _is_turn_completed_event(normalized_type, payload)
            and turn_id is not None
            and self._active_turn_id == turn_id
        ):
            self._active_turn_id = None

        resolved_turn = turn_id or self._active_turn_id
        if item_id is not None and resolved_turn is not None:
            self._item_to_turn[item_id] = resolved_turn
            if turn_id is None:
                turn_id = resolved_turn
        if request_id is not None and resolved_turn is not None:
            self._request_to_turn[request_id] = resolved_turn
            if turn_id is None:
                turn_id = resolved_turn

        stale_after_interrupt = _extract_bool(payload, self._STALE_KEYS) is True
        if turn_id is not None and turn_id in self._superseded_turn_ids:
            stale_after_interrupt = True

        return CausalEnvelopeFields(
            turn_id=turn_id,
            item_id=item_id,
            request_id=request_id,
            interrupt_epoch=self._interrupt_epoch,
            stale_after_interrupt=stale_after_interrupt,
        )

    def _extract_turn_id(self, normalized_type: str, payload: dict[str, object]) -> str | None:
        turn_id = _extract_str(payload, self._TURN_ID_KEYS)
        if turn_id is not None:
            return turn_id
        if _looks_like_turn_event(normalized_type):
            fallback = _extract_str(payload, ("id",))
            if fallback is not None:
                return fallback
        return None

    def _extract_item_id(self, normalized_type: str, payload: dict[str, object]) -> str | None:
        item_id = _extract_str(payload, self._ITEM_ID_KEYS)
        if item_id is not None:
            return item_id
        if ".item." in f".{normalized_type}.":
            fallback = _extract_str(payload, ("id",))
            if fallback is not None:
                return fallback
        return None

    def _extract_request_id(self, normalized_type: str, payload: dict[str, object]) -> str | None:
        request_id = _extract_str(payload, self._REQUEST_ID_KEYS)
        if request_id is not None:
            return request_id
        if "request" in normalized_type:
            fallback = _extract_str(payload, ("id",))
            if fallback is not None:
                return fallback
        return None


class ManagedPrimaryReconciliationStrategy:
    """Managed-primary reconciliation policy."""

    @staticmethod
    def supports(snapshot: ManagedPrimarySnapshot | None) -> bool:
        """Return whether this strategy handles the given snapshot."""

        return snapshot is not None and snapshot.metadata.managed_backend

    @staticmethod
    def decide(
        context: ReconciliationContext,
        *,
        has_recent_activity: bool,
        durable_report_completion: bool,
    ) -> ReconciliationDecision:
        """Decide reconciliation outcome for a managed primary."""

        # Import here to avoid circular dependency.
        from meridian.lib.state.reaper import (
            FinalizeFailed,
            FinalizeSucceededFromReport,
            Skip,
        )

        managed = context.managed_snapshot

        if managed.launcher_pid_alive:
            return Skip(reason="primary_launcher_alive")

        if managed.metadata.activity == "finalizing":
            if has_recent_activity:
                return Skip(reason="recent_activity")
            if durable_report_completion:
                return FinalizeSucceededFromReport()
            return FinalizeFailed(error="orphan_finalization")

        return FinalizeFailed(error="orphan_primary")


def read_managed_primary_snapshot(
    runtime_root: Path,
    record: SpawnRecord,
    *,
    started_epoch: float | None = None,
) -> ManagedPrimarySnapshot | None:
    """Read managed primary snapshot for reconciliation decisions."""

    metadata = read_primary_metadata(runtime_root, record.id)
    if metadata is None or not metadata.managed_backend:
        return None

    launcher_pid_alive = False
    if metadata.launcher_pid is not None:
        launcher_pid_alive = is_process_alive(
            metadata.launcher_pid,
            created_after_epoch=started_epoch,
        )

    return ManagedPrimarySnapshot(
        metadata=metadata,
        launcher_pid_alive=launcher_pid_alive,
        started_epoch=started_epoch,
    )


def _terminate_pid(pid: int) -> bool:
    """Terminate a single process by PID using psutil."""

    if pid <= 0 or pid == os.getpid():
        return False
    try:
        psutil.Process(pid).terminate()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False
    return True


def terminate_managed_primary_processes(
    primary_metadata: PrimaryMetadata | None,
    *,
    started_epoch: float | None = None,
    include_launcher: bool,
    include_runtime_children: bool = True,
) -> tuple[int, ...]:
    """Best-effort SIGTERM for tracked managed-primary processes."""

    if primary_metadata is None or not primary_metadata.managed_backend:
        return ()

    if include_launcher and include_runtime_children:
        candidates = (
            primary_metadata.launcher_pid,
            primary_metadata.backend_pid,
            primary_metadata.tui_pid,
        )
    elif include_launcher:
        candidates = (primary_metadata.launcher_pid,)
    else:
        candidates = (
            primary_metadata.backend_pid,
            primary_metadata.tui_pid,
        )

    signaled: list[int] = []
    seen: set[int] = set()
    for candidate in candidates:
        if candidate is None or candidate in seen:
            continue
        seen.add(candidate)
        if not is_process_alive(candidate, created_after_epoch=started_epoch):
            continue
        if _terminate_pid(candidate):
            signaled.append(candidate)
    return tuple(signaled)


def _normalize_event_type(event_type: str) -> str:
    return event_type.strip().lower().replace("/", ".")


def _looks_like_turn_event(normalized_type: str) -> bool:
    tokenized = f".{normalized_type}."
    return ".turn." in tokenized


def _is_turn_started_event(normalized_type: str, payload: dict[str, object]) -> bool:
    if normalized_type in {"turn.started", "turn.start", "turn/started"}:
        return True
    if normalized_type == "turn":
        state = _extract_str(payload, ("state", "status", "phase"))
        return state in {"started", "start", "begin"}
    return normalized_type.endswith("turn.started")


def _is_turn_completed_event(normalized_type: str, payload: dict[str, object]) -> bool:
    if normalized_type in {"turn.completed", "turn.complete", "turn/complete", "turn/completed"}:
        return True
    if normalized_type == "turn":
        state = _extract_str(payload, ("state", "status", "phase"))
        return state in {"completed", "complete", "done", "stopped"}
    return normalized_type.endswith("turn.completed")


def _is_interrupt_request_event(normalized_type: str, payload: dict[str, object]) -> bool:
    status = _extract_str(payload, ("status",))
    kind = _extract_str(payload, ("kind", "action"))

    if kind == "interrupt" and status == "requested":
        return True
    if normalized_type in {"control.interrupt.requested", "interrupt.requested"}:
        return True
    return "interrupt" in normalized_type and status == "requested"


def _extract_bool(payload: dict[str, object], keys: tuple[str, ...]) -> bool | None:
    for scope in _iter_scopes(payload):
        for key in keys:
            raw = scope.get(key)
            if isinstance(raw, bool):
                return raw
    return None


def _extract_int(payload: dict[str, object], keys: tuple[str, ...]) -> int | None:
    for scope in _iter_scopes(payload):
        for key in keys:
            raw = scope.get(key)
            if isinstance(raw, int):
                return raw
    return None


def _extract_str(payload: dict[str, object], keys: tuple[str, ...]) -> str | None:
    for scope in _iter_scopes(payload):
        for key in keys:
            raw = scope.get(key)
            if isinstance(raw, str):
                stripped = raw.strip()
                if stripped:
                    return stripped
    return None


def _iter_scopes(payload: dict[str, object]) -> tuple[Mapping[str, object], ...]:
    scopes: list[Mapping[str, object]] = [payload]
    for key in NESTED_SCOPE_KEYS:
        candidate = payload.get(key)
        if isinstance(candidate, Mapping):
            scopes.append(cast("Mapping[str, object]", candidate))
    return tuple(scopes)


__all__ = [
    "CausalEnvelopeFields",
    "ManagedPrimaryCausalTracker",
    "ManagedPrimaryReconciliationStrategy",
    "ManagedPrimarySnapshot",
    "ReconciliationContext",
    "read_managed_primary_snapshot",
    "terminate_managed_primary_processes",
]
