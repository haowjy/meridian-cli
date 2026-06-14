from __future__ import annotations

import asyncio
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close, CloseCode

from meridian.lib.harness.connections.codex_ws import CodexConnection


class _ClosingWebSocket:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __aiter__(self) -> _ClosingWebSocket:
        return self

    async def __anext__(self) -> Any:
        raise self._exc


@pytest.mark.asyncio
async def test_codex_reader_treats_normal_close_error_as_clean_shutdown() -> None:
    connection = CodexConnection()
    connection._state = "connected"
    connection._ws = _ClosingWebSocket(
        ConnectionClosedError(
            None,
            Close(CloseCode.NORMAL_CLOSURE, ""),
            None,
        )
    )

    task = asyncio.create_task(connection._read_messages_loop())
    await task

    assert task.exception() is None
    assert await connection._event_queue.get() is None


@pytest.mark.asyncio
async def test_codex_reader_reports_abnormal_close() -> None:
    connection = CodexConnection()
    connection._state = "connected"
    connection._ws = _ClosingWebSocket(
        ConnectionClosedError(
            Close(CloseCode.INTERNAL_ERROR, "boom"),
            None,
            None,
        )
    )

    await connection._read_messages_loop()

    event = await connection._event_queue.get()
    assert event is not None
    assert event.event_type == "error/connectionClosed"
    assert "1011" in str(event.payload["message"])
    assert await connection._event_queue.get() is None
