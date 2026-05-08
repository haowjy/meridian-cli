from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    ConnectionState,
    HarnessConnection,
    HarnessEvent,
    HarnessRequest,
)
from meridian.lib.harness.permission_broker import PermissionBroker, PermissionTransition


class _DummyConnection(HarnessConnection[Any]):
    def __init__(self) -> None:
        self.responses: list[tuple[str, str, dict[str, object] | None]] = []
        self.user_inputs: list[tuple[str, dict[str, object]]] = []

    @property
    def state(self) -> ConnectionState:
        return "connected"

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.CODEX

    @property
    def spawn_id(self) -> SpawnId:
        return SpawnId("p-perm")

    @property
    def capabilities(self) -> ConnectionCapabilities:
        return ConnectionCapabilities(
            mid_turn_injection="interrupt_restart",
            supports_steer=True,
            supports_cancel=True,
            runtime_model_switch=False,
            structured_reasoning=True,
            supports_runtime_hitl=True,
        )

    @property
    def session_id(self) -> str | None:
        return "session-perm"

    @property
    def subprocess_pid(self) -> int | None:
        return None

    async def start(self, config: ConnectionConfig, spec: Any) -> None:
        _ = config, spec

    async def stop(self) -> None:
        return None

    def health(self) -> bool:
        return True

    async def send_user_message(self, text: str) -> None:
        _ = text

    async def send_cancel(self) -> None:
        return None

    async def respond_request(
        self,
        request_id: str,
        decision: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        self.responses.append((request_id, decision, payload))

    async def respond_user_input(
        self,
        request_id: str,
        answers: dict[str, object],
    ) -> None:
        self.user_inputs.append((request_id, answers))

    async def events(self) -> AsyncIterator[HarnessEvent]:
        if False:
            yield HarnessEvent(event_type="never", payload={}, harness_id="codex")


def _journal_statuses(path: Path) -> list[str]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [row["status"] for row in rows]


@pytest.mark.asyncio
async def test_permission_broker_pending_request_emits_and_persists_without_auto_response(
    tmp_path: Path,
) -> None:
    events: list[HarnessEvent] = []

    async def _event_sink(event: HarnessEvent) -> None:
        events.append(event)

    broker = PermissionBroker(spawn_dir=tmp_path, event_sink=_event_sink)
    connection = _DummyConnection()

    await broker.handle_request(
        connection,
        HarnessRequest(
            request_id="approval-1",
            request_type="approval",
            method="item/commandExecution/requestApproval",
            payload={"command": "make test"},
        ),
    )

    record = broker.get_request("approval-1")
    assert record is not None
    assert record.status == "pending"
    assert events == [
        HarnessEvent(
            event_type="request/opened",
            payload={
                "request_id": "approval-1",
                "request_type": "approval",
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "make test"},
            },
            harness_id="codex",
            raw_text=None,
        )
    ]
    assert connection.responses == []
    assert connection.user_inputs == []
    assert _journal_statuses(tmp_path / "permission_requests.jsonl") == ["pending"]


@pytest.mark.asyncio
async def test_permission_broker_tracks_resolved_failed_and_cancelled_states(
    tmp_path: Path,
) -> None:
    broker = PermissionBroker(spawn_dir=tmp_path)
    connection = _DummyConnection()

    await broker.handle_request(
        connection,
        HarnessRequest(
            request_id="r-resolved",
            request_type="approval",
            method="item/commandExecution/requestApproval",
            payload={"command": "true"},
        ),
    )
    await broker.on_request_resolved("r-resolved", resolution={"decision": "accept"})

    await broker.handle_request(
        connection,
        HarnessRequest(
            request_id="r-failed",
            request_type="approval",
            method="item/commandExecution/requestApproval",
            payload={"command": "false"},
        ),
    )
    await broker.on_request_failed("r-failed", error="connection closed")

    await broker.handle_request(
        connection,
        HarnessRequest(
            request_id="r-cancelled",
            request_type="user_input",
            method="item/tool/requestUserInput",
            payload={"schema": {"type": "object"}},
        ),
    )
    await broker.on_requests_cancelled(["r-cancelled"], reason="connection_stopped")

    resolved = broker.get_request("r-resolved")
    failed = broker.get_request("r-failed")
    cancelled = broker.get_request("r-cancelled")
    assert resolved is not None and resolved.status == "resolved"
    assert resolved.resolution == {"decision": "accept"}
    assert failed is not None and failed.status == "failed"
    assert failed.error == "connection closed"
    assert cancelled is not None and cancelled.status == "cancelled"
    assert cancelled.error == "connection_stopped"
    assert _journal_statuses(tmp_path / "permission_requests.jsonl") == [
        "pending",
        "resolved",
        "pending",
        "failed",
        "pending",
        "cancelled",
    ]


@pytest.mark.asyncio
async def test_permission_broker_cursor_prevents_replay_refiring_resolved_decisions(
    tmp_path: Path,
) -> None:
    hook_calls: list[PermissionTransition] = []

    async def _policy_hook(transition: PermissionTransition) -> None:
        hook_calls.append(transition)

    broker = PermissionBroker(spawn_dir=tmp_path, policy_hook=_policy_hook)
    connection = _DummyConnection()

    await broker.handle_request(
        connection,
        HarnessRequest(
            request_id="r1",
            request_type="approval",
            method="item/commandExecution/requestApproval",
            payload={"command": "echo hi"},
        ),
    )
    await broker.on_request_resolved("r1", resolution={"decision": "accept"})

    assert [call.status for call in hook_calls] == ["pending", "resolved"]

    resumed = PermissionBroker(spawn_dir=tmp_path)
    replayed: list[PermissionTransition] = []

    async def _replay_sink(transition: PermissionTransition) -> None:
        replayed.append(transition)

    delivered = await resumed.replay_for_consumer(
        consumer_id="policy_hook",
        sink=_replay_sink,
    )

    assert delivered == 0
    assert replayed == []
