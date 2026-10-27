"""Durable broker for runtime approval and user-input requests."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import (
    HarnessConnection,
    HarnessRequest,
    RawHarnessEvent,
    ServerRequestHandler,
)
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.permission_journal import (
    REJECT_DECISION,
    TERMINAL_STATUSES,
    PermissionJournal,
    PermissionRequestRecord,
    PermissionRequestStatus,
    PermissionRequestType,
    PermissionTransition,
)
from meridian.lib.state.spawn_aggregate import mutate_published_spawn_artifact

# Why a headless spawn's request was refused. Only OpenCode's V2 reply carries it to the
# model; every refusal is journaled with it.
HEADLESS_REJECT_REASON = "Needs approval, and a headless spawn has no one to ask. Do not retry it."

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
        event_sink: Callable[[RawHarnessEvent], Awaitable[None]] | None = None,
        policy_hook: PermissionTransitionSink | None = None,
        auto_reject_runtime_requests: bool = False,
        harness_id: str = "codex",
    ) -> None:
        self._runtime_root = spawn_dir.parent.parent
        self._spawn_id = SpawnId(spawn_dir.name)
        self._event_sink = event_sink
        self._policy_hook = policy_hook
        self._auto_reject_runtime_requests = auto_reject_runtime_requests
        self._harness_id = harness_id
        self._cursor_path = spawn_dir / "permission_request_cursors.json"

        self._lock = asyncio.Lock()
        self._journal = PermissionJournal(spawn_dir)
        self._consumer_cursors: dict[str, int] = {}

        self._load_cursors()

    async def handle_request(
        self,
        connection: HarnessConnection[Any],
        request: HarnessRequest,
    ) -> None:
        async with self._lock:
            existing = self._journal.get_request(request.request_id)
            if existing is not None:
                if existing.status in TERMINAL_STATUSES:
                    return
                if existing.status == "pending":
                    return

            transition = self._journal.append(
                request_id=request.request_id,
                request_type=request.request_type,
                method=request.method,
                payload=request.payload,
                status="pending",
                summary=request.summary,
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
            await connection.respond_request(
                request.request_id,
                REJECT_DECISION,
                {"message": HEADLESS_REJECT_REASON},
            )
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
            resolution={"decision": REJECT_DECISION, "message": HEADLESS_REJECT_REASON},
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
                record = self._journal.get_request(request_id)
                if record is None or record.status in TERMINAL_STATUSES:
                    continue
                transitions.append(
                    self._journal.append(
                        request_id=record.request_id,
                        request_type=record.request_type,
                        method=record.method,
                        payload=record.payload,
                        status="cancelled",
                        summary=record.summary,
                        error=reason,
                    )
                )
        for transition in transitions:
            await self._dispatch_transition(transition)

    def get_request(self, request_id: str) -> PermissionRequestRecord | None:
        return self._journal.get_request(request_id)

    def list_requests(self) -> list[PermissionRequestRecord]:
        return self._journal.list_requests()

    def list_transitions(self) -> list[PermissionTransition]:
        return self._journal.list_transitions()

    async def _transition_existing_request(
        self,
        *,
        request_id: str,
        status: PermissionRequestStatus,
        resolution: dict[str, object] | None = None,
        error: str | None = None,
    ) -> PermissionTransition | None:
        async with self._lock:
            record = self._journal.get_request(request_id)
            if record is None:
                return None
            if record.status in TERMINAL_STATUSES:
                return None
            return self._journal.append(
                request_id=record.request_id,
                request_type=record.request_type,
                method=record.method,
                payload=record.payload,
                status=status,
                summary=record.summary,
                resolution=resolution,
                error=error,
            )

    async def _dispatch_transition(self, transition: PermissionTransition) -> None:
        event = _transition_to_event(transition, harness_id=self._harness_id)
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
        mutate_published_spawn_artifact(
            self._runtime_root,
            self._spawn_id,
            lambda: atomic_write_text(
                self._cursor_path,
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            ),
        )


def _transition_to_event(
    transition: PermissionTransition,
    *,
    harness_id: str = "codex",
) -> RawHarnessEvent | None:
    payload: dict[str, object] = {
        "request_id": transition.request_id,
        "request_type": transition.request_type,
        "method": transition.method,
    }
    if transition.status == "pending":
        payload["params"] = transition.payload
        return RawHarnessEvent(
            event_type="request/opened",
            payload=payload,
            harness_id=harness_id,
            raw_text=None,
        )
    if transition.status == "resolved":
        if transition.resolution:
            payload.update(transition.resolution)
        return RawHarnessEvent(
            event_type="request/resolved",
            payload=payload,
            harness_id=harness_id,
            raw_text=None,
        )
    if transition.status == "failed":
        if transition.error is not None:
            payload["error"] = transition.error
        return RawHarnessEvent(
            event_type="request/failed",
            payload=payload,
            harness_id=harness_id,
            raw_text=None,
        )
    if transition.status == "cancelled":
        if transition.error is not None:
            payload["reason"] = transition.error
        return RawHarnessEvent(
            event_type="request/cancelled",
            payload=payload,
            harness_id=harness_id,
            raw_text=None,
        )
    return None


__all__ = [
    "PermissionBroker",
    "PermissionRequestRecord",
    "PermissionRequestStatus",
    "PermissionRequestType",
    "PermissionTransition",
]
