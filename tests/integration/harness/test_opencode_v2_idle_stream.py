"""OpenCode V2 managed-primary event stream and idle-sensor integration."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness import opencode_idle
from meridian.lib.harness.connections import opencode_http
from meridian.lib.harness.connections.base import HarnessConnection, RawHarnessEvent
from meridian.lib.harness.connections.opencode_v2_http import OpenCodeV2Connection
from meridian.lib.harness.idle_types import IdleEvent, IdleSensorContext
from tests.support.async_determinism import AsyncDeterminism, wait_until

_SESSION_ID = "ses_v2_stream"


class _QueuedContent:
    def __init__(self) -> None:
        self._chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self.reads = 0

    async def read(self, _size: int) -> bytes:
        self.reads += 1
        return await self._chunks.get()

    def feed(self, *chunks: bytes) -> None:
        for chunk in chunks:
            self._chunks.put_nowait(chunk)


class _QueuedResponse:
    def __init__(self) -> None:
        self.content = _QueuedContent()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _StreamProbeConnection(OpenCodeV2Connection):
    def __init__(self, responses: list[_QueuedResponse]) -> None:
        super().__init__()
        self._state = "connected"
        self._session_id = _SESSION_ID
        self._process = cast("Any", SimpleNamespace(pid=os.getpid(), returncode=None))
        self._responses = iter(responses)
        self.open_count = 0

    async def _session_still_running(self) -> bool | None:
        # This observer's turn has ended: the quiet stream reconnects.
        return False

    async def _open_event_stream(self) -> _QueuedResponse:
        self.open_count += 1
        try:
            return next(self._responses)
        except StopIteration as exc:
            raise AssertionError("unexpected extra event-stream reconnect") from exc


def _sse(event_type: str, **data: object) -> bytes:
    envelope = {
        "id": f"evt_{event_type}",
        "type": event_type,
        "data": {"sessionID": _SESSION_ID, **data},
        "durable": {"aggregateID": _SESSION_ID},
    }
    return f"data: {json.dumps(envelope)}\n\n".encode()


def _sensor_context(tmp_path: Path, connection: _StreamProbeConnection) -> IdleSensorContext:
    return IdleSensorContext(
        connection=cast("HarnessConnection[Any]", connection),
        harness_id=HarnessId.OPENCODE,
        harness_session_id=_SESSION_ID,
        env={"MERIDIAN_HARNESS_OPENCODE_VERSION": "v2"},
        tmux_pane=None,
        tui_alive=lambda: True,
        spawn_dir=tmp_path,
    )


@pytest.mark.asyncio
async def test_v2_managed_primary_stream_survives_idle_reconnect_and_later_user_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    determinism = AsyncDeterminism(start=1_000.0)
    determinism.install(monkeypatch, monotonic_modules=(opencode_http,))
    determinism.install_on_running_loop(monkeypatch)

    first_stream = _QueuedResponse()
    reconnected_stream = _QueuedResponse()
    connection = _StreamProbeConnection([first_stream, reconnected_stream])
    connection._primary_observer_mode = True

    async def request(
        method: str,
        path: str,
        _payload: Mapping[str, object] | None,
    ) -> tuple[int, str]:
        session_path = f"/api/session/{_SESSION_ID}"
        if (method, path) == ("GET", session_path):
            return 200, json.dumps(
                {
                    "data": {
                        "id": _SESSION_ID,
                        "model": {"providerID": "opencode-go", "id": "deepseek-v4.1-flash"},
                    }
                }
            )
        assert (method, path) == ("GET", f"{session_path}/message?limit=20&order=desc")
        return 200, '{"data":[]}'

    sensor = opencode_idle.OpenCodeV2IdleSensor(
        _sensor_context(tmp_path, connection),
        request=request,
    )
    idle_events: list[IdleEvent] = []
    raw_events: list[RawHarnessEvent] = []

    async def consume_connection() -> None:
        async for event in connection.events():
            raw_events.append(event)
            sensor.on_raw_event(event)

    async def consume_sensor() -> None:
        async for event in sensor.events():
            idle_events.append(event)

    connection_task = asyncio.create_task(consume_connection())
    sensor_task = asyncio.create_task(consume_sensor())
    try:
        first_stream.content.feed(
            _sse("session.execution.started"),
            _sse("session.execution.succeeded"),
        )
        await wait_until(
            lambda: len(raw_events) == 2 and first_stream.content.reads >= 3,
            description="completed first turn and resumed SSE read",
        )

        determinism.advance(121.0)
        await wait_until(
            lambda: connection.open_count == 2,
            description="event stream reconnect after more than 120 seconds quiet",
        )
        assert not connection_task.done()

        reconnected_stream.content.feed(
            _sse(
                "session.inbox.enqueued",
                inboxID="msg_later_user",
                item={"type": "user", "payload": {"text": "later turn"}, "delivery": "steer"},
            )
        )
        await wait_until(
            lambda: any(event.kind == "user_prompt" for event in idle_events),
            description="later user turn",
        )

        assert [event.kind for event in idle_events] == ["turn_end", "user_prompt"]
        assert raw_events[-1].event_type == "session.inbox.enqueued"
    finally:
        connection._state = "stopped"
        for task in (connection_task, sensor_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(connection_task, sensor_task, return_exceptions=True)
