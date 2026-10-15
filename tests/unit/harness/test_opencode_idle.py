from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness import opencode_idle
from meridian.lib.harness.connections.base import HarnessConnection, RawHarnessEvent
from meridian.lib.harness.connections.opencode_v2_http import OpenCodeV2Connection
from meridian.lib.harness.idle_types import CacheTtl, IdleEvent, IdleSensorContext

FIXTURES = Path(__file__).parents[2] / "fixtures" / "opencode_idle"
SESSION_ID = "ses_ee21d9d97ffeN9m0VxoYy7qFQf"
V2_SESSION_ID = "ses_v2_fixture_primary"


class FakeConnection:
    observer_endpoint = None

    def __init__(self, resolved_version: str | None = None) -> None:
        self.resolved_version = resolved_version


class FakeClock:
    def __init__(self, now: float) -> None:
        self.value = now

    def __call__(self) -> float:
        return self.value


def _context(
    tmp_path: Path,
    *,
    session_id: str = SESSION_ID,
    version: str | None = None,
    resolved_version: str | None = None,
) -> IdleSensorContext:
    env = {} if version is None else {"MERIDIAN_HARNESS_OPENCODE_VERSION": version}
    return IdleSensorContext(
        connection=cast("HarnessConnection[Any]", FakeConnection(resolved_version)),
        harness_id=HarnessId.OPENCODE,
        harness_session_id=session_id,
        env=env,
        tmux_pane="%7",
        tui_alive=lambda: True,
        spawn_dir=tmp_path,
    )


def _recorded_events(name: str) -> list[RawHarnessEvent]:
    events: list[RawHarnessEvent] = []
    for data in _sse_data_lines(name):
        payload = json.loads(data)
        events.append(
            RawHarnessEvent(
                event_type=str(payload["type"]),
                payload=payload,
                harness_id="opencode",
                raw_text=data,
            )
        )
    return events


def _sse_data_lines(name: str) -> Iterator[str]:
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        if line.startswith("data: "):
            yield line.removeprefix("data: ")


def _recorded_v2_events(name: str) -> list[RawHarnessEvent]:
    connection = OpenCodeV2Connection()
    events: list[RawHarnessEvent] = []
    for data in _sse_data_lines(name):
        event = connection._event_from_json_line(data, raw_text=data)
        assert event is not None
        events.append(event)
    return events


async def _take(iterator: AsyncIterator[IdleEvent], count: int) -> list[IdleEvent]:
    return [await anext(iterator) for _ in range(count)]


def _v2_raw(event_type: str, **payload: object) -> RawHarnessEvent:
    return RawHarnessEvent(
        event_type=event_type,
        payload={"type": event_type, "sessionID": V2_SESSION_ID, **payload},
        harness_id="opencode",
    )


def _v2_user_enqueued(inbox_id: str, text: str, delivery: str = "queue") -> RawHarnessEvent:
    return _v2_raw(
        "session.inbox.enqueued",
        inboxID=inbox_id,
        item={"type": "user", "payload": {"text": text}, "delivery": delivery},
    )


def _v2_sensor(
    tmp_path: Path, **kwargs: Any
) -> tuple[opencode_idle.OpenCodeV2IdleSensor, AsyncIterator[IdleEvent]]:
    sensor = opencode_idle.OpenCodeV2IdleSensor(
        _context(tmp_path, session_id=V2_SESSION_ID, version="v2"), **kwargs
    )
    return sensor, sensor.events()


async def _assert_quiet(iterator: AsyncIterator[IdleEvent]) -> None:
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(iterator), timeout=0.01)


def _v2_request_stub(
    *,
    provider: str,
    model_id: str,
    compaction_id: str,
    on_compact: Callable[[], None],
) -> opencode_idle.BackendRequest:
    async def request(
        method: str, path: str, payload: Mapping[str, object] | None
    ) -> tuple[int, str]:
        session_path = f"/api/session/{V2_SESSION_ID}"
        if (method, path) == ("GET", session_path):
            return 200, json.dumps(
                {"data": {"id": V2_SESSION_ID, "model": {"providerID": provider, "id": model_id}}}
            )
        if (method, path) == ("GET", f"{session_path}/message?limit=20&order=desc"):
            return 200, '{"data":[]}'
        assert (method, path, payload) == ("POST", f"{session_path}/compact", {})
        on_compact()
        return 200, json.dumps(
            {"data": {"id": compaction_id, "sessionID": V2_SESSION_ID, "type": "compaction"}}
        )

    return request


async def test_recorded_normal_prompt_emits_one_return_and_one_turn_end(
    tmp_path: Path,
) -> None:
    sensor = opencode_idle.OpenCodeIdleSensor(_context(tmp_path), now=lambda: 1_791_502_764.0)
    iterator = sensor.events()

    for event in _recorded_events("events-normal.sse"):
        sensor.on_raw_event(event)

    observed = await _take(iterator, 2)
    assert [event.kind for event in observed] == ["user_prompt", "turn_end"]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(iterator), timeout=0.01)


async def test_turn_end_fetches_last_user_and_assistant_text_parts(tmp_path: Path) -> None:
    async def request(
        method: str,
        path: str,
        _payload: Mapping[str, object] | None,
    ) -> tuple[int, str]:
        assert (method, path) == ("GET", f"/session/{SESSION_ID}/message?limit=20")
        return 200, json.dumps(
            [
                {"info": {"role": "user"}, "parts": [{"type": "text", "text": "old"}]},
                {
                    "info": {"role": "user"},
                    "parts": [
                        {"type": "text", "text": "attached file", "synthetic": True},
                        {"type": "text", "text": "ignored prompt", "ignored": 1},
                    ],
                },
                {
                    "info": {"role": "assistant"},
                    "parts": [
                        {"type": "reasoning", "text": "private"},
                        {"type": "text", "text": "answer one"},
                        {"type": "text", "text": "answer two"},
                    ],
                },
                {
                    "info": {"role": "user"},
                    "parts": [
                        {"type": "text", "text": "latest"},
                        {"type": "text", "text": "mode reminder", "synthetic": True},
                        {"type": "text", "text": "hidden", "ignored": True},
                    ],
                },
            ]
        )

    sensor = opencode_idle.OpenCodeIdleSensor(
        _context(tmp_path),
        now=lambda: 1_791_502_764.0,
        request=request,
    )
    iterator = sensor.events()
    for event in _recorded_events("events-normal.sse"):
        sensor.on_raw_event(event)

    observed = await _take(iterator, 2)
    turn_end = observed[-1]
    assert turn_end.last_user_text == "latest"
    assert turn_end.last_assistant_text == "answer one answer two"


async def test_recorded_summarize_never_emits_user_return(tmp_path: Path) -> None:
    sensor = opencode_idle.OpenCodeIdleSensor(_context(tmp_path), now=lambda: 1_791_502_786.0)
    iterator = sensor.events()

    for event in _recorded_events("events-summarize.sse"):
        sensor.on_raw_event(event)

    assert [event.kind for event in await _take(iterator, 1)] == ["turn_end"]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(iterator), timeout=0.01)


async def test_compact_suppresses_return_when_busy_precedes_compaction_part(
    tmp_path: Path,
) -> None:
    post_started = asyncio.Event()
    release_post = asyncio.Event()

    async def request(
        method: str,
        _path: str,
        _payload: Mapping[str, object] | None,
    ) -> tuple[int, str]:
        if method == "GET":
            return 200, '{"model":{"providerID":"opencode","id":"big-pickle"}}'
        post_started.set()
        await release_post.wait()
        return 200, "true"

    sensor = opencode_idle.OpenCodeIdleSensor(
        _context(tmp_path),
        now=FakeClock(1_791_502_764.0),
        request=request,
    )
    iterator = sensor.events()
    events = _recorded_events("events-summarize.sse")
    user_message = next(event for event in events if event.event_type == "message.updated")
    compaction_part = next(
        event
        for event in events
        if event.event_type == "message.part.updated"
        and cast("Mapping[str, object]", event.payload["properties"])["part"]
        and cast(
            "Mapping[str, object]",
            cast("Mapping[str, object]", event.payload["properties"])["part"],
        )["type"]
        == "compaction"
    )
    first_busy = next(
        event
        for event in events
        if event.event_type == "session.status"
        and cast(
            "Mapping[str, object]",
            cast("Mapping[str, object]", event.payload["properties"])["status"],
        )["type"]
        == "busy"
    )
    final_idle = next(
        event
        for event in events
        if event.event_type == "session.status"
        and cast(
            "Mapping[str, object]",
            cast("Mapping[str, object]", event.payload["properties"])["status"],
        )["type"]
        == "idle"
    )
    session_idle = next(event for event in events if event.event_type == "session.idle")

    compact = asyncio.create_task(sensor.compact())
    await post_started.wait()
    for event in (user_message, first_busy, compaction_part):
        sensor.on_raw_event(event)
    sensor.on_raw_event(final_idle)
    sensor.on_raw_event(session_idle)
    release_post.set()
    assert (await compact).result == "ok"

    real_user = next(
        event
        for event in _recorded_events("events-normal.sse")
        if event.event_type == "message.updated"
    )
    sensor.on_raw_event(real_user)
    sensor.on_raw_event(first_busy)

    observed = await asyncio.wait_for(_take(iterator, 2), timeout=0.1)
    assert [event.kind for event in observed] == ["turn_end", "user_prompt"]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(iterator), timeout=0.01)


@pytest.mark.parametrize(
    "terminal_type",
    [
        "session.execution.succeeded",
        "session.execution.failed",
        "session.execution.interrupted",
    ],
)
async def test_v2_recorded_normal_prompt_emits_return_and_parent_turn_end(
    tmp_path: Path, terminal_type: str
) -> None:
    request = _v2_request_stub(
        provider="openai",
        model_id="gpt-6-astra",
        compaction_id="unused",
        on_compact=lambda: None,
    )
    sensor, iterator = _v2_sensor(tmp_path, request=request)
    events = _recorded_v2_events("events-v2-normal.sse")
    terminal_id = cast("str", events[-1].payload["id"])
    events[-1] = _v2_raw(terminal_type, id=terminal_id)
    sensor.on_raw_event(_v2_raw(terminal_type, id=terminal_id, sessionID="ses_child_task"))
    for event in events:
        sensor.on_raw_event(event)
    observed = await _take(iterator, 3)
    assert [event.kind for event in observed] == ["user_prompt", "user_prompt", "turn_end"]
    assert (observed[-1].turn_id, observed[-1].provider) == (terminal_id, "openai")
    await _assert_quiet(iterator)


def test_v2_turn_end_fetches_wrapped_descending_message_excerpts() -> None:
    payload = {
        "data": [
            {"type": "assistant", "content": "latest answer"},
            {"type": "user", "text": "latest"},
            {"type": "assistant", "content": "old answer"},
            {"type": "user", "text": "old prompt"},
        ]
    }
    assert opencode_idle._v2_message_excerpts(payload) == ("latest", "latest answer")


async def test_v2_steered_prompt_returns_before_terminal_and_does_not_leak_to_compaction(
    tmp_path: Path,
) -> None:
    sensor, iterator = _v2_sensor(tmp_path)
    normal = _recorded_v2_events("events-v2-normal.sse")
    steered = _v2_user_enqueued(
        "msg_v2_user_steered", "follow up while the turn is running", "steer"
    )
    for event in (*normal[:2], steered, *normal[2:]):
        sensor.on_raw_event(event)
    for event in _recorded_v2_events("events-v2-compact.sse"):
        sensor.on_raw_event(event)
    observed = await _take(iterator, 5)
    assert [event.kind for event in observed] == ["user_prompt"] * 3 + ["turn_end"] * 2
    await _assert_quiet(iterator)


async def test_v2_queued_prompt_returns_when_delivered_for_second_execution(
    tmp_path: Path,
) -> None:
    sensor, iterator = _v2_sensor(tmp_path)
    delivered_id = "msg_v2_user_queued"
    cancelled_id = "msg_v2_user_cancelled"
    sensor.on_raw_event(_v2_raw("session.execution.started"))
    sensor.on_raw_event(_v2_user_enqueued(delivered_id, "follow up after this turn"))
    sensor.on_raw_event(_v2_user_enqueued(cancelled_id, "never mind"))
    sensor.on_raw_event(_v2_raw("session.inbox.cancelled", inboxID=cancelled_id))
    sensor.on_raw_event(_v2_raw("session.execution.succeeded"))
    assert [event.kind for event in await _take(iterator, 3)] == ["user_prompt"] * 2 + ["turn_end"]
    sensor.on_raw_event(_v2_raw("session.execution.started"))
    sensor.on_raw_event(_v2_raw("session.inbox.delivered", inboxID=cancelled_id))
    sensor.on_raw_event(_v2_raw("session.inbox.delivered", inboxID=delivered_id))
    sensor.on_raw_event(_v2_raw("session.execution.succeeded"))
    assert [event.kind for event in await _take(iterator, 2)] == ["user_prompt", "turn_end"]
    await _assert_quiet(iterator)


@pytest.mark.parametrize(
    ("terminal_type", "expected_result"),
    [
        ("session.execution.succeeded", "ok"),
        ("session.execution.failed", "failed"),
        ("session.execution.interrupted", "failed"),
    ],
)
async def test_v2_compact_waits_for_matching_recorded_completion(
    tmp_path: Path, terminal_type: str, expected_result: str
) -> None:
    sensor: opencode_idle.OpenCodeV2IdleSensor

    def on_compact() -> None:
        events = _recorded_v2_events("events-v2-compact.sse")
        if terminal_type != "session.execution.succeeded":
            events[-1] = _v2_raw(terminal_type, id=events[-1].payload["id"])
        for event in events:
            sensor.on_raw_event(event)

    request = _v2_request_stub(
        provider="opencode-go",
        model_id="deepseek-v4.1-flash",
        compaction_id="msg_v2_compaction",
        on_compact=on_compact,
    )
    sensor, iterator = _v2_sensor(tmp_path, request=request, compact_timeout_seconds=0.1)
    result = await sensor.compact()
    assert result.result == expected_result
    assert result.reason == (None if expected_result == "ok" else terminal_type)
    assert [event.kind for event in await _take(iterator, 1)] == ["turn_end"]
    await _assert_quiet(iterator)


@pytest.mark.parametrize(
    ("compaction_status", "expected_result"),
    [("completed", "ok"), ("failed", "failed")],
)
async def test_v2_compact_confirms_durable_completion_after_stream_gap(
    tmp_path: Path,
    compaction_status: str,
    expected_result: str,
) -> None:
    compact_posted = False
    session_path = f"/api/session/{V2_SESSION_ID}"

    async def request(
        method: str, path: str, payload: Mapping[str, object] | None
    ) -> tuple[int, str]:
        nonlocal compact_posted
        if (method, path) == ("GET", session_path):
            return 200, json.dumps(
                {
                    "data": {
                        "id": V2_SESSION_ID,
                        "model": {"providerID": "opencode-go", "id": "deepseek-v4.1-flash"},
                        "outcome": "succeeded" if compact_posted else None,
                        "time": {"idle": 1_700_000_001_000} if compact_posted else {},
                    }
                }
            )
        if method == "GET" and path == (
            f"{session_path}/message?limit=20&order=desc&type=compaction"
        ):
            return 200, json.dumps(
                {
                    "data": [
                        {
                            "id": "msg_v2_compaction",
                            "type": "compaction",
                            "status": compaction_status,
                        }
                    ]
                }
            )
        assert (method, path, payload) == ("POST", f"{session_path}/compact", {})
        compact_posted = True
        return 200, json.dumps(
            {
                "data": {
                    "id": "msg_v2_compaction",
                    "sessionID": V2_SESSION_ID,
                    "time": {"created": 1_700_000_000_000},
                    "type": "compaction",
                }
            }
        )

    sensor, _ = _v2_sensor(tmp_path, request=request, compact_timeout_seconds=0.01)

    result = await sensor.compact()

    assert result.result == expected_result
    assert result.reason == (None if expected_result == "ok" else "timeout")


@pytest.mark.parametrize(
    ("version", "resolved_version", "expected_type"),
    [
        ("v1", "v1", opencode_idle.OpenCodeIdleSensor),
        ("v1", None, opencode_idle.OpenCodeIdleSensor),
        ("v2", "v2", opencode_idle.OpenCodeV2IdleSensor),
        ("auto", "v1", opencode_idle.OpenCodeIdleSensor),
        ("auto", "v2", opencode_idle.OpenCodeV2IdleSensor),
    ],
)
def test_primary_idle_sensor_dispatches_by_resolved_connection_version(
    tmp_path: Path,
    version: str | None,
    resolved_version: str | None,
    expected_type: type[object],
) -> None:
    sensor = opencode_idle.primary_idle_sensor(
        _context(tmp_path, version=version, resolved_version=resolved_version)
    )

    assert type(sensor) is expected_type


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("openai", CacheTtl(1800, estimated=True)),
        ("opencode-go", None),
    ],
)
def test_detect_ttl_by_provider(provider: str, expected: CacheTtl | None) -> None:
    actual = opencode_idle.detect_ttl(
        session_id=V2_SESSION_ID, native_store=None, provider=provider, env={}
    )
    assert actual == expected
