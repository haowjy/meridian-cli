"""CodexConnection event-stream liveness regression tests."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest

from meridian.lib.harness.connections import codex_ws
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.connections.codex_ws import CodexConnection
from tests.support.async_determinism import AsyncDeterminism


class _FakeProcess:
    pid = 4242
    returncode: int | None = None


class _ControllableProcess:
    pid = 4243

    def __init__(self) -> None:
        self.returncode: int | None = None
        self._exited = asyncio.Event()
        self.wait_cancelled = False

    async def wait(self) -> int:
        try:
            await self._exited.wait()
        except asyncio.CancelledError:
            self.wait_cancelled = True
            raise
        assert self.returncode is not None
        return self.returncode

    def exit(self, returncode: int) -> None:
        self.returncode = returncode
        self._exited.set()


class _RecordingScopeHandle:
    def __init__(self, process: _ControllableProcess | None = None) -> None:
        self.terminate_calls = 0
        self._process = process

    async def terminate(self, *, grace_seconds: float, reason: str) -> None:
        _ = reason
        assert grace_seconds > 0
        self.terminate_calls += 1
        if self._process is not None:
            self._process.exit(-15)


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


class _FailingWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self.fail = asyncio.Event()

    def __aiter__(self) -> _FailingWebSocket:
        return self

    async def __anext__(self) -> str:
        await self.fail.wait()
        raise RuntimeError("socket disappeared")

    async def close(self) -> None:
        self.closed = True
        self.fail.set()


async def _collect_events(connection: CodexConnection) -> list[RawHarnessEvent]:
    return [event async for event in connection.events()]


def _drain_raw_event_queue(
    connection: CodexConnection,
) -> list[RawHarnessEvent | None]:
    queued: list[RawHarnessEvent | None] = []
    while True:
        try:
            queued.append(connection._event_queue.get_nowait())
        except asyncio.QueueEmpty:
            return queued


async def _collect_codex_events_under_fake_clock(
    determinism: AsyncDeterminism,
    connection: CodexConnection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    advance_budget: float = 1.0,
    step: float = 0.01,
) -> list[RawHarnessEvent]:
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
        "meridian/error/connectionClosed",
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


@pytest.mark.asyncio
async def test_codex_backend_exit_and_reader_race_emit_one_enriched_close(
    tmp_path: Path,
) -> None:
    connection = CodexConnection()
    connection._state = "connected"
    process = _ControllableProcess()
    connection._process = process  # type: ignore[assignment]
    websocket = _FailingWebSocket()
    connection._ws = websocket
    stderr_path = tmp_path / "stderr.log"
    stderr_path.write_text("fatal vendor detail\n", encoding="utf-8")
    connection._stderr_log_path = stderr_path

    reader = asyncio.create_task(connection._read_messages_loop())
    watcher = asyncio.create_task(connection._watch_backend_exit())
    websocket.fail.set()
    process.exit(23)

    await asyncio.gather(reader, watcher)
    terminal_sequence = _drain_raw_event_queue(connection)

    assert len(terminal_sequence) == 2
    close_event = terminal_sequence[0]
    assert close_event is not None
    assert close_event.event_type == "meridian/error/connectionClosed"
    assert close_event.payload["message"] == (
        "Codex app-server exited with code 23.\n\n"
        "Codex app-server stderr:\nfatal vendor detail"
    )
    assert close_event.payload["backend_exit_code"] == 23
    assert close_event.payload["backend_stderr_excerpt"] == "fatal vendor detail"
    assert terminal_sequence[1] is None
    assert connection._event_queue.empty()
    assert connection.state == "failed"


@pytest.mark.asyncio
async def test_codex_backend_exit_watcher_closes_lingering_websocket(
    tmp_path: Path,
) -> None:
    connection = CodexConnection()
    connection._state = "connected"
    process = _ControllableProcess()
    connection._process = process  # type: ignore[assignment]
    websocket = _FreezesAfterMessagesWebSocket([])
    connection._ws = websocket
    stderr_path = tmp_path / "stderr.log"
    stderr_path.write_text("watcher-only failure\n", encoding="utf-8")
    connection._stderr_log_path = stderr_path
    reader = asyncio.create_task(connection._read_messages_loop())
    watcher = asyncio.create_task(connection._watch_backend_exit())
    await asyncio.sleep(0)

    process.exit(31)
    await watcher
    terminal_sequence = _drain_raw_event_queue(connection)
    reader.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await reader

    assert len(terminal_sequence) == 2
    close_event = terminal_sequence[0]
    assert close_event is not None
    assert close_event.payload["backend_exit_code"] == 31
    assert close_event.payload["backend_stderr_excerpt"] == "watcher-only failure"
    assert terminal_sequence[1] is None
    assert connection._event_queue.empty()


@pytest.mark.asyncio
async def test_codex_expected_stop_cancels_watcher_before_backend_termination() -> None:
    connection = CodexConnection()
    connection._state = "connected"
    process = _ControllableProcess()
    connection._process = process  # type: ignore[assignment]
    websocket = _FailingWebSocket()
    connection._ws = websocket
    scope_handle = _RecordingScopeHandle(process)
    connection._scope_handle = scope_handle  # type: ignore[assignment]
    connection._backend_exit_task = asyncio.create_task(
        connection._watch_backend_exit()
    )
    connection._reader_task = asyncio.create_task(connection._read_messages_loop())
    await asyncio.sleep(0)

    await connection.stop(reason="test")
    events = await _collect_events(connection)

    assert events == []
    assert process.wait_cancelled is True
    assert scope_handle.terminate_calls == 1
    assert connection.state == "stopped"


@pytest.mark.asyncio
async def test_codex_cleanup_failure_still_ends_expected_stop_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_parent_death_cleanup(_link: object) -> None:
        raise OSError("watchdog reap failed")

    monkeypatch.setattr(
        codex_ws,
        "release_parent_death_link",
        _fail_parent_death_cleanup,
    )
    connection = CodexConnection()
    connection._state = "connected"

    await connection.stop(reason="test")

    assert connection.state == "stopped"
    assert connection._event_stream_ended is True
    assert _drain_raw_event_queue(connection) == [None]
    assert connection._event_queue.empty()
