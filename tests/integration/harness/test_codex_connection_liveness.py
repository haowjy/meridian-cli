"""CodexConnection event-stream liveness regression tests."""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from meridian.lib.harness.connections import codex_ws
from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.harness.connections.codex_ws import CodexConnection
from tests.support.async_determinism import AsyncDeterminism


class _FakeProcess:
    pid = 4242
    returncode: int | None = None


class _RecordingScopeHandle:
    def __init__(self) -> None:
        self.terminate_calls = 0

    async def terminate(self, *, grace_seconds: float, reason: str) -> None:
        _ = reason
        assert grace_seconds > 0
        self.terminate_calls += 1


class _FreezesAfterMessagesWebSocket:
    def __init__(self, messages: list[str]) -> None:
        self._messages = messages
        self.closed = False

    def __aiter__(self) -> _FreezesAfterMessagesWebSocket:
        return self

    async def __anext__(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True


async def _collect_events(connection: CodexConnection) -> list[HarnessEvent]:
    return [event async for event in connection.events()]


async def _collect_codex_events_under_fake_clock(
    determinism: AsyncDeterminism,
    connection: CodexConnection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    advance_budget: float = 1.0,
    step: float = 0.01,
) -> list[HarnessEvent]:
    determinism.install_on_running_loop(monkeypatch)
    reader_task = asyncio.create_task(connection._read_messages_loop())
    collect_task = asyncio.create_task(_collect_events(connection))
    advanced = 0.0
    while not collect_task.done() and advanced < advance_budget:
        await determinism.sleep(step)
        advanced += step
    assert collect_task.done(), (
        f"event collection did not finish within fake-clock budget {advance_budget}"
    )
    events = await collect_task
    await reader_task
    return events


@pytest.mark.asyncio
async def test_codex_events_fail_after_liveness_timeout_mid_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    determinism = AsyncDeterminism(start=0.0)
    monkeypatch.setattr(codex_ws._time, "monotonic", determinism.clock.monotonic)
    determinism.install(monkeypatch)
    connection = CodexConnection()
    connection._state = "connected"
    connection._process = _FakeProcess()
    connection._ws = _FreezesAfterMessagesWebSocket(
        [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "item/updated",
                    "params": {"id": "item-1"},
                }
            )
        ]
    )
    monkeypatch.setattr(CodexConnection, "_LIVENESS_TIMEOUT_SECONDS", 0.01)

    events = await _collect_codex_events_under_fake_clock(
        determinism, connection, monkeypatch
    )

    assert [event.event_type for event in events] == [
        "item/updated",
        "error/connectionClosed",
    ]
    assert "liveness timeout" in str(events[-1].payload["message"])
    assert connection.state == "failed"


@pytest.mark.asyncio
async def test_codex_health_fails_when_event_liveness_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = CodexConnection()
    connection._state = "connected"
    connection._process = _FakeProcess()
    connection._ws = _FreezesAfterMessagesWebSocket([])

    monkeypatch.setattr(CodexConnection, "_LIVENESS_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(codex_ws._time, "monotonic", lambda: 10.0)
    connection._liveness.mark_activity()

    monkeypatch.setattr(codex_ws._time, "monotonic", lambda: 10.5)
    assert connection.health() is True

    monkeypatch.setattr(codex_ws._time, "monotonic", lambda: 11.1)
    assert connection.health() is False


@pytest.mark.asyncio
async def test_codex_stop_terminates_scope_when_reader_task_already_failed() -> None:
    async def _failed_reader() -> None:
        raise RuntimeError("reader failed before stop")

    connection = CodexConnection()
    connection._state = "connected"
    connection._process = _FakeProcess()
    scope_handle = _RecordingScopeHandle()
    connection._scope_handle = scope_handle  # type: ignore[assignment]
    reader_task = asyncio.create_task(_failed_reader())
    with contextlib.suppress(RuntimeError):
        await reader_task
    connection._reader_task = reader_task

    await connection.stop(reason="test")

    assert scope_handle.terminate_calls == 1
    assert connection.state == "stopped"
