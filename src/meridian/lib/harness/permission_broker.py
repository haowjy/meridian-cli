"""Durable broker for runtime approval and user-input requests."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from meridian.lib.harness.connections.base import (
    HarnessConnection,
    HarnessEvent,
    HarnessRequest,
    ServerRequestHandler,
)
from meridian.lib.state.atomic import append_text_line, atomic_write_text

PermissionRequestType = Literal["approval", "user_input"]
PermissionRequestStatus = Literal["pending", "resolved", "failed", "cancelled"]
_TERMINAL_STATUSES: frozenset[PermissionRequestStatus] = frozenset(
    {"resolved", "failed", "cancelled"}
)


@dataclass(frozen=True)
class PermissionRequestRecord:
    """Current state snapshot for one permission/user-input request."""

    request_id: str
    request_type: PermissionRequestType
    method: str
    payload: dict[str, object]
    status: PermissionRequestStatus
    opened_seq: int
    updated_seq: int
    resolution: dict[str, object] | None = None
    error: str | None = None


@dataclass(frozen=True)
class PermissionTransition:
    """One durable transition entry from the permission broker journal."""

    seq: int
    request_id: str
    request_type: PermissionRequestType
    method: str
    payload: dict[str, object]
    status: PermissionRequestStatus
    resolution: dict[str, object] | None
    error: str | None
    timestamp: str


PermissionTransitionSink = Callable[[PermissionTransition], Awaitable[None]]
logger = logging.getLogger(__name__)


class PermissionBroker(ServerRequestHandler):
    """Route runtime requests to UI/policy sinks with durable state transitions."""

    no_runtime_hitl: bool = False

    _EVENT_CONSUMER_ID = "runtime_event_sink"
    _POLICY_CONSUMER_ID = "policy_hook"

    def __init__(
        self,
        *,
        spawn_dir: Path,
        event_sink: Callable[[HarnessEvent], Awaitable[None]] | None = None,
        policy_hook: PermissionTransitionSink | None = None,
        auto_reject_runtime_requests: bool = False,
    ) -> None:
        self._spawn_dir = spawn_dir
        self._event_sink = event_sink
        self._policy_hook = policy_hook
        self._auto_reject_runtime_requests = auto_reject_runtime_requests
        self._journal_path = self._spawn_dir / "permission_requests.jsonl"
        self._cursor_path = self._spawn_dir / "permission_request_cursors.json"

        self._lock = asyncio.Lock()
        self._requests: dict[str, PermissionRequestRecord] = {}
        self._transitions: list[PermissionTransition] = []
        self._consumer_cursors: dict[str, int] = {}
        self._next_seq = 0

        self._load_journal()
        self._load_cursors()

    async def handle_request(
        self,
        connection: HarnessConnection[Any],
        request: HarnessRequest,
    ) -> None:
        async with self._lock:
            existing = self._requests.get(request.request_id)
            if existing is not None:
                if existing.status in _TERMINAL_STATUSES:
                    return
                if existing.status == "pending":
                    return

            transition = self._record_transition_locked(
                request_id=request.request_id,
                request_type=request.request_type,
                method=request.method,
                payload=request.payload,
                status="pending",
            )

        await self._dispatch_transition(transition)
        if self._auto_reject_runtime_requests:
            await self._auto_reject_request(connection, request)

    async def _auto_reject_request(
        self,
        connection: HarnessConnection[Any],
        request: HarnessRequest,
    ) -> None:
        try:
            await connection.respond_request(request.request_id, "reject")
        except Exception as exc:
            logger.warning(
                "Failed to auto-reject runtime request %s",
                request.request_id,
                exc_info=True,
            )
            await self.on_request_failed(request.request_id, error=str(exc))
            return

        record = self.get_request(request.request_id)
        if record is None or record.status != "pending":
            return
        await self.on_request_resolved(
            request.request_id,
            resolution={"decision": "reject"},
        )

    async def on_request_resolved(
        self,
        request_id: str,
        *,
        resolution: dict[str, object] | None = None,
    ) -> None:
        transition = await self._transition_existing_request(
            request_id=request_id,
            status="resolved",
            resolution=resolution,
        )
        if transition is not None:
            await self._dispatch_transition(transition)

    async def on_request_failed(
        self,
        request_id: str,
        *,
        error: str,
    ) -> None:
        transition = await self._transition_existing_request(
            request_id=request_id,
            status="failed",
            error=error,
        )
        if transition is not None:
            await self._dispatch_transition(transition)

    async def on_requests_cancelled(
        self,
        request_ids: list[str],
        *,
        reason: str,
    ) -> None:
        transitions: list[PermissionTransition] = []
        async with self._lock:
            for request_id in request_ids:
                record = self._requests.get(request_id)
                if record is None or record.status in _TERMINAL_STATUSES:
                    continue
                transitions.append(
                    self._record_transition_locked(
                        request_id=record.request_id,
                        request_type=record.request_type,
                        method=record.method,
                        payload=record.payload,
                        status="cancelled",
                        error=reason,
                    )
                )
        for transition in transitions:
            await self._dispatch_transition(transition)

    async def replay_for_consumer(
        self,
        *,
        consumer_id: str,
        sink: PermissionTransitionSink,
    ) -> int:
        async with self._lock:
            cursor = self._consumer_cursors.get(consumer_id, -1)
            transitions = [
                transition for transition in self._transitions if transition.seq > cursor
            ]

        delivered = 0
        for transition in transitions:
            await sink(transition)
            delivered += 1
            await self._advance_cursor(consumer_id=consumer_id, seq=transition.seq)
        return delivered

    def get_request(self, request_id: str) -> PermissionRequestRecord | None:
        return self._requests.get(request_id)

    def list_requests(self) -> list[PermissionRequestRecord]:
        return sorted(self._requests.values(), key=lambda item: item.opened_seq)

    def list_transitions(self) -> list[PermissionTransition]:
        return list(self._transitions)

    async def _transition_existing_request(
        self,
        *,
        request_id: str,
        status: PermissionRequestStatus,
        resolution: dict[str, object] | None = None,
        error: str | None = None,
    ) -> PermissionTransition | None:
        async with self._lock:
            record = self._requests.get(request_id)
            if record is None:
                return None
            if record.status in _TERMINAL_STATUSES:
                return None
            return self._record_transition_locked(
                request_id=record.request_id,
                request_type=record.request_type,
                method=record.method,
                payload=record.payload,
                status=status,
                resolution=resolution,
                error=error,
            )

    async def _dispatch_transition(self, transition: PermissionTransition) -> None:
        event = _transition_to_event(transition)
        event_sink = self._event_sink
        if event is not None and event_sink is not None:
            await self._dispatch_with_cursor(
                consumer_id=self._EVENT_CONSUMER_ID,
                seq=transition.seq,
                dispatch=lambda: event_sink(event),
            )
        policy_hook = self._policy_hook
        if policy_hook is not None:
            await self._dispatch_with_cursor(
                consumer_id=self._POLICY_CONSUMER_ID,
                seq=transition.seq,
                dispatch=lambda: policy_hook(transition),
            )

    async def _dispatch_with_cursor(
        self,
        *,
        consumer_id: str,
        seq: int,
        dispatch: Callable[[], Awaitable[None]],
    ) -> None:
        async with self._lock:
            if seq <= self._consumer_cursors.get(consumer_id, -1):
                return
        await dispatch()
        await self._advance_cursor(consumer_id=consumer_id, seq=seq)

    async def _advance_cursor(self, *, consumer_id: str, seq: int) -> None:
        async with self._lock:
            current = self._consumer_cursors.get(consumer_id, -1)
            if seq <= current:
                return
            self._consumer_cursors[consumer_id] = seq
            self._write_cursors_locked()

    def _record_transition_locked(
        self,
        *,
        request_id: str,
        request_type: PermissionRequestType,
        method: str,
        payload: dict[str, object],
        status: PermissionRequestStatus,
        resolution: dict[str, object] | None = None,
        error: str | None = None,
    ) -> PermissionTransition:
        seq = self._next_seq
        self._next_seq += 1
        transition = PermissionTransition(
            seq=seq,
            request_id=request_id,
            request_type=request_type,
            method=method,
            payload=dict(payload),
            status=status,
            resolution=dict(resolution) if resolution is not None else None,
            error=error,
            timestamp=_utc_now_iso(),
        )
        line = json.dumps(_transition_to_json(transition), sort_keys=True, separators=(",", ":"))
        append_text_line(self._journal_path, line + "\n")
        self._transitions.append(transition)
        self._apply_transition(transition)
        return transition

    def _apply_transition(self, transition: PermissionTransition) -> None:
        existing = self._requests.get(transition.request_id)
        opened_seq = transition.seq if existing is None else existing.opened_seq
        self._requests[transition.request_id] = PermissionRequestRecord(
            request_id=transition.request_id,
            request_type=transition.request_type,
            method=transition.method,
            payload=dict(transition.payload),
            status=transition.status,
            opened_seq=opened_seq,
            updated_seq=transition.seq,
            resolution=dict(transition.resolution) if transition.resolution is not None else None,
            error=transition.error,
        )

    def _load_journal(self) -> None:
        if not self._journal_path.exists():
            return
        for line in self._journal_path.read_text(encoding="utf-8").splitlines():
            parsed = _parse_transition_line(line)
            if parsed is None:
                continue
            self._transitions.append(parsed)

        self._transitions.sort(key=lambda transition: transition.seq)
        for transition in self._transitions:
            self._apply_transition(transition)
        if self._transitions:
            self._next_seq = self._transitions[-1].seq + 1

    def _load_cursors(self) -> None:
        if not self._cursor_path.exists():
            return
        try:
            payload_obj = json.loads(self._cursor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload_obj, dict):
            return
        payload = cast("dict[str, object]", payload_obj)
        consumers_obj = payload.get("consumers")
        if not isinstance(consumers_obj, dict):
            return
        for key, value in cast("dict[object, object]", consumers_obj).items():
            if not isinstance(key, str) or not isinstance(value, int):
                continue
            self._consumer_cursors[key] = value

    def _write_cursors_locked(self) -> None:
        payload = {
            "consumers": self._consumer_cursors,
        }
        atomic_write_text(
            self._cursor_path,
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        )


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _transition_to_event(transition: PermissionTransition) -> HarnessEvent | None:
    payload: dict[str, object] = {
        "request_id": transition.request_id,
        "request_type": transition.request_type,
        "method": transition.method,
    }
    if transition.status == "pending":
        payload["params"] = transition.payload
        return HarnessEvent(
            event_type="request/opened",
            payload=payload,
            harness_id="codex",
            raw_text=None,
        )
    if transition.status == "resolved":
        if transition.resolution:
            payload.update(transition.resolution)
        return HarnessEvent(
            event_type="request/resolved",
            payload=payload,
            harness_id="codex",
            raw_text=None,
        )
    if transition.status == "failed":
        if transition.error is not None:
            payload["error"] = transition.error
        return HarnessEvent(
            event_type="request/failed",
            payload=payload,
            harness_id="codex",
            raw_text=None,
        )
    if transition.status == "cancelled":
        if transition.error is not None:
            payload["reason"] = transition.error
        return HarnessEvent(
            event_type="request/cancelled",
            payload=payload,
            harness_id="codex",
            raw_text=None,
        )
    return None


def _transition_to_json(transition: PermissionTransition) -> dict[str, object]:
    return {
        "seq": transition.seq,
        "request_id": transition.request_id,
        "request_type": transition.request_type,
        "method": transition.method,
        "payload": transition.payload,
        "status": transition.status,
        "resolution": transition.resolution,
        "error": transition.error,
        "timestamp": transition.timestamp,
    }


def _parse_transition_line(line: str) -> PermissionTransition | None:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        payload_obj = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload_obj, dict):
        return None
    payload = cast("dict[str, object]", payload_obj)

    seq = payload.get("seq")
    request_id = payload.get("request_id")
    request_type = payload.get("request_type")
    method = payload.get("method")
    request_payload = payload.get("payload")
    status = payload.get("status")
    resolution = payload.get("resolution")
    error = payload.get("error")
    timestamp = payload.get("timestamp")

    if not isinstance(seq, int):
        return None
    if not isinstance(request_id, str) or not request_id:
        return None
    if request_type not in ("approval", "user_input"):
        return None
    if not isinstance(method, str) or not method:
        return None
    if not isinstance(request_payload, dict):
        return None
    if status not in ("pending", "resolved", "failed", "cancelled"):
        return None
    if resolution is not None and not isinstance(resolution, dict):
        return None
    if error is not None and not isinstance(error, str):
        return None
    if not isinstance(timestamp, str) or not timestamp:
        return None

    typed_request_type: PermissionRequestType = request_type
    typed_status: PermissionRequestStatus = status
    return PermissionTransition(
        seq=seq,
        request_id=request_id,
        request_type=typed_request_type,
        method=method,
        payload=cast("dict[str, object]", request_payload),
        status=typed_status,
        resolution=cast("dict[str, object] | None", resolution),
        error=error,
        timestamp=timestamp,
    )


__all__ = [
    "PermissionBroker",
    "PermissionRequestRecord",
    "PermissionRequestStatus",
    "PermissionRequestType",
    "PermissionTransition",
]
