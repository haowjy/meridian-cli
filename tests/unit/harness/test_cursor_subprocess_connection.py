"""Cursor subprocess connection parsing and shutdown tests."""

from __future__ import annotations

import inspect

import pytest

from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.harness.connections.cursor_subprocess import CursorSubprocessConnection


class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""


class _FakeProcess:
    def __init__(
        self,
        *,
        lines: list[bytes],
        returncode: int | None,
        wait_returncode: int = 0,
        kill_error: BaseException | None = None,
    ) -> None:
        self.stdout = _FakeStdout(lines)
        self.returncode = returncode
        self.wait_returncode = wait_returncode
        self.kill_error = kill_error
        self.terminate_called = False
        self.kill_called = False
        self.wait_calls = 0
        self.pid = 42

    async def wait(self) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            self.returncode = self.wait_returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminate_called = True

    def kill(self) -> None:
        self.kill_called = True
        if self.kill_error is not None:
            raise self.kill_error


async def _collect_events(connection: CursorSubprocessConnection) -> list[HarnessEvent]:
    return [event async for event in connection.events()]


@pytest.mark.asyncio
async def test_cursor_events_skip_invalid_lines_after_protocol_validation() -> None:
    connection = CursorSubprocessConnection()
    connection._state = "connected"  # pyright: ignore[reportPrivateUsage]
    connection._process = _FakeProcess(  # pyright: ignore[reportPrivateUsage]
        lines=[
            b'{"type":"system","sessionId":"ses-1"}\n',
            b'{"oops":\n',
            b'{"type":"result","subtype":"success"}\n',
        ],
        returncode=0,
    )

    events = await _collect_events(connection)

    assert [event.event_type for event in events] == ["system", "result"]
    assert connection.state == "connected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_line",
    [
        b'{"oops":\n',
        b'["not-object"]\n',
        b'{"message":"missing type"}\n',
    ],
)
async def test_cursor_events_emit_protocol_mismatch_when_first_payload_invalid(
    bad_line: bytes,
) -> None:
    connection = CursorSubprocessConnection()
    connection._state = "connected"  # pyright: ignore[reportPrivateUsage]
    connection._process = _FakeProcess(  # pyright: ignore[reportPrivateUsage]
        lines=[bad_line],
        returncode=0,
    )

    events = await _collect_events(connection)

    assert len(events) == 1
    assert events[0].event_type == "error/connectionClosed"
    assert "protocol mismatch" in str(events[0].payload.get("message", "")).lower()
    assert connection.state == "failed"


@pytest.mark.asyncio
async def test_cursor_events_emit_error_on_nonzero_exit() -> None:
    connection = CursorSubprocessConnection()
    connection._state = "connected"  # pyright: ignore[reportPrivateUsage]
    connection._process = _FakeProcess(  # pyright: ignore[reportPrivateUsage]
        lines=[],
        returncode=7,
    )

    events = await _collect_events(connection)

    assert len(events) == 1
    assert events[0].event_type == "error/connectionClosed"
    assert events[0].payload["message"] == "Cursor subprocess exited with code 7."
    assert connection.state == "failed"


@pytest.mark.asyncio
async def test_cursor_send_cancel_handles_process_lookup_kill_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _timeout_wait_for(
        awaitable: object,
        timeout: float,
    ) -> int:
        _ = timeout
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(
        "meridian.lib.harness.connections.cursor_subprocess.asyncio.wait_for",
        _timeout_wait_for,
    )

    process = _FakeProcess(
        lines=[],
        returncode=None,
        kill_error=ProcessLookupError(),
    )
    connection = CursorSubprocessConnection()
    connection._state = "connected"  # pyright: ignore[reportPrivateUsage]
    connection._process = process  # pyright: ignore[reportPrivateUsage]

    await connection.send_cancel()

    assert process.terminate_called is True
    assert process.kill_called is True
    assert connection.subprocess_pid is None
