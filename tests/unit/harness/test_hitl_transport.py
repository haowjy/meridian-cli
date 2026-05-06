# pyright: reportPrivateUsage=false
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import (
    AutoAcceptHandler,
    ConnectionCapabilities,
    ConnectionConfig,
    ConnectionState,
    HarnessConnection,
    HarnessEvent,
    HarnessRequest,
    InteractiveHandler,
)
from meridian.lib.harness.connections.claude_ws import ClaudeConnection
from meridian.lib.harness.connections.codex_ws import CodexConnection
from meridian.lib.harness.connections.opencode_http import OpenCodeConnection


class _RecordingConnection(HarnessConnection[Any]):
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
        return SpawnId("p-hitl")

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
        return "thread-hitl"

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


@pytest.mark.asyncio
async def test_auto_accept_handler_auto_accepts_approval_and_answers_user_input() -> None:
    connection = _RecordingConnection()
    handler = AutoAcceptHandler()

    await handler.handle_request(
        connection,
        HarnessRequest(
            request_id="approval-1",
            request_type="approval",
            method="item/commandExecution/requestApproval",
            payload={"command": "true"},
        ),
    )
    await handler.handle_request(
        connection,
        HarnessRequest(
            request_id="input-1",
            request_type="user_input",
            method="item/tool/requestUserInput",
            payload={"schema": {}},
        ),
    )

    assert connection.responses == [("approval-1", "accept", None)]
    assert connection.user_inputs == [("input-1", {})]


@pytest.mark.asyncio
async def test_interactive_handler_emits_request_opened_event() -> None:
    connection = _RecordingConnection()
    events: list[HarnessEvent] = []

    async def _sink(event: HarnessEvent) -> None:
        events.append(event)

    handler = InteractiveHandler(_sink)

    await handler.handle_request(
        connection,
        HarnessRequest(
            request_id="approval-2",
            request_type="approval",
            method="item/commandExecution/requestApproval",
            payload={"command": "make test"},
        ),
    )

    assert events == [
        HarnessEvent(
            event_type="request/opened",
            payload={
                "request_id": "approval-2",
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


def test_auto_accept_handler_has_no_runtime_hitl_true() -> None:
    assert AutoAcceptHandler.no_runtime_hitl is True


@pytest.mark.asyncio
async def test_codex_respond_request_sends_jsonrpc_result_and_clears_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = CodexConnection()
    connection._hitl_requests["approval-3"] = 42
    sent_payloads: list[dict[str, object]] = []

    async def _fake_send_json(payload: dict[str, object]) -> None:
        sent_payloads.append(payload)

    monkeypatch.setattr(connection, "_send_json", _fake_send_json)

    await connection.respond_request("approval-3", "reject", {"reason": "no"})

    assert sent_payloads == [
        {
            "jsonrpc": "2.0",
            "id": 42,
            "result": {"decision": "reject", "reason": "no"},
        }
    ]
    assert connection._hitl_requests == {}


@pytest.mark.asyncio
async def test_codex_default_handler_preserves_auto_accept_spawn_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = CodexConnection()
    sent_results: list[tuple[object, dict[str, object]]] = []

    async def _fake_send_jsonrpc_result(
        request_id: object,
        result: dict[str, object],
    ) -> None:
        sent_results.append((request_id, result))

    monkeypatch.setattr(connection, "_send_jsonrpc_result", _fake_send_jsonrpc_result)

    await connection._handle_server_request(
        {
            "id": "approval-rpc-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "true"},
        }
    )
    await connection._handle_server_request(
        {
            "id": "input-rpc-1",
            "method": "item/tool/requestUserInput",
            "params": {"schema": {}},
        }
    )

    assert sent_results == [
        ("approval-rpc-1", {"decision": "accept"}),
        ("input-rpc-1", {"answers": {}}),
    ]
    assert connection._hitl_requests == {}


@pytest.mark.asyncio
async def test_unsupported_harnesses_raise_for_runtime_hitl() -> None:
    for connection in (ClaudeConnection(), OpenCodeConnection()):
        assert connection.capabilities.supports_runtime_hitl is False
        with pytest.raises(NotImplementedError, match="does not support runtime request"):
            await connection.respond_request("r1", "accept")
        with pytest.raises(NotImplementedError, match="does not support runtime user input"):
            await connection.respond_user_input("r2", {})
