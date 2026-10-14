from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness import opencode_idle
from meridian.lib.harness.connections.base import HarnessConnection, RawHarnessEvent
from meridian.lib.harness.connections.opencode_v2_http import OpenCodeV2Connection
from meridian.lib.harness.idle_types import IdleEvent, IdleSensorContext

FIXTURES = Path(__file__).parents[2] / "fixtures" / "opencode_idle"
SESSION_ID = "ses_ee21d9d97ffeN9m0VxoYy7qFQf"
V2_SESSION_ID = "ses_v2_fixture_primary"


class FakeConnection:
    observer_endpoint = None


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
) -> IdleSensorContext:
    env = {} if version is None else {"MERIDIAN_HARNESS_OPENCODE_VERSION": version}
    return IdleSensorContext(
        connection=cast("HarnessConnection[Any]", FakeConnection()),
        harness_id=HarnessId.OPENCODE,
        harness_session_id=session_id,
        env=env,
        tmux_pane="%7",
        tui_alive=lambda: True,
        spawn_dir=tmp_path,
    )


def _recorded_events(name: str) -> list[RawHarnessEvent]:
    events: list[RawHarnessEvent] = []
    for block in (FIXTURES / name).read_text(encoding="utf-8").split("\n\n"):
        data = next(
            (
                line.removeprefix("data: ")
                for line in block.splitlines()
                if line.startswith("data: ")
            ),
            None,
        )
        if data is None:
            continue
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


def _recorded_v2_events(name: str) -> list[RawHarnessEvent]:
    connection = OpenCodeV2Connection()
    events: list[RawHarnessEvent] = []
    for block in (FIXTURES / name).read_text(encoding="utf-8").split("\n\n"):
        data = next(
            (
                line.removeprefix("data: ")
                for line in block.splitlines()
                if line.startswith("data: ")
            ),
            None,
        )
        if data is None:
            continue
        event = connection._event_from_json_line(data, raw_text=data)
        assert event is not None
        events.append(event)
    return events


async def _take(iterator: AsyncIterator[IdleEvent], count: int) -> list[IdleEvent]:
    return [await anext(iterator) for _ in range(count)]


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
    ("fixture", "expected_draft"),
    [("pane-draft-typed.txt", "yes"), ("pane-draft-empty.txt", "no")],
)
async def test_facts_read_draft_from_recorded_pane(
    tmp_path: Path,
    fixture: str,
    expected_draft: str,
) -> None:
    async def capture(_pane: str) -> str | None:
        return (FIXTURES / fixture).read_text(encoding="utf-8")

    facts = await opencode_idle.OpenCodeIdleSensor(_context(tmp_path), capture_pane=capture).facts()

    assert (facts.draft, facts.busy) == (expected_draft, False)


async def test_v2_recorded_normal_prompt_emits_return_and_parent_turn_end(
    tmp_path: Path,
) -> None:
    sensor = opencode_idle.OpenCodeV2IdleSensor(
        _context(tmp_path, session_id=V2_SESSION_ID, version="v2")
    )
    iterator = sensor.events()
    events = _recorded_v2_events("events-v2-normal.sse")
    terminal = events[-1]

    sensor.on_raw_event(
        RawHarnessEvent(
            event_type=terminal.event_type,
            payload={**terminal.payload, "sessionID": "ses_child_task"},
            harness_id=terminal.harness_id,
            raw_text=terminal.raw_text,
        )
    )
    for event in events:
        sensor.on_raw_event(event)

    observed = await _take(iterator, 2)
    assert [event.kind for event in observed] == ["user_prompt", "turn_end"]
    assert observed[0].implies_return is True
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(iterator), timeout=0.01)


async def test_v2_turn_end_fetches_wrapped_descending_message_excerpts(
    tmp_path: Path,
) -> None:
    async def request(
        method: str,
        path: str,
        _payload: Mapping[str, object] | None,
    ) -> tuple[int, str]:
        assert (method, path) == (
            "GET",
            f"/api/session/{V2_SESSION_ID}/message?limit=20&order=desc",
        )
        return 200, json.dumps(
            {
                "data": [
                    {
                        "id": "assistant-new",
                        "type": "assistant",
                        "content": [
                            {"type": "reasoning", "text": "private"},
                            {"type": "text", "text": "answer one"},
                            {"type": "text", "text": "answer two"},
                        ],
                    },
                    {"id": "user-new", "type": "user", "text": "latest"},
                    {
                        "id": "assistant-old",
                        "type": "assistant",
                        "content": [{"type": "text", "text": "old answer"}],
                    },
                    {"id": "user-old", "type": "user", "text": "old prompt"},
                ],
                "cursor": {"previous": None, "next": None},
            }
        )

    sensor = opencode_idle.OpenCodeV2IdleSensor(
        _context(tmp_path, session_id=V2_SESSION_ID, version="v2"),
        request=request,
    )
    iterator = sensor.events()
    for event in _recorded_v2_events("events-v2-normal.sse"):
        sensor.on_raw_event(event)

    turn_end = (await _take(iterator, 2))[-1]
    assert turn_end.last_user_text == "latest"
    assert turn_end.last_assistant_text == "answer one answer two"


async def test_v2_recorded_compaction_never_emits_user_return(tmp_path: Path) -> None:
    sensor = opencode_idle.OpenCodeV2IdleSensor(
        _context(tmp_path, session_id=V2_SESSION_ID, version="v2")
    )
    iterator = sensor.events()

    for event in _recorded_v2_events("events-v2-compact.sse"):
        sensor.on_raw_event(event)

    assert [event.kind for event in await _take(iterator, 1)] == ["turn_end"]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(iterator), timeout=0.01)


async def test_v2_compact_waits_for_matching_recorded_completion(tmp_path: Path) -> None:
    sensor: opencode_idle.OpenCodeV2IdleSensor

    async def request(
        method: str,
        path: str,
        payload: Mapping[str, object] | None,
    ) -> tuple[int, str]:
        if method == "GET":
            if path.endswith("/message?limit=20&order=desc"):
                return 200, '{"data":[],"cursor":{"previous":null,"next":null}}'
            assert path == f"/api/session/{V2_SESSION_ID}"
            return 200, json.dumps(
                {
                    "data": {
                        "id": V2_SESSION_ID,
                        "model": {
                            "providerID": "opencode-go",
                            "id": "deepseek-v4.1-flash",
                        },
                    }
                }
            )
        assert (method, path, payload) == (
            "POST",
            f"/api/session/{V2_SESSION_ID}/compact",
            {},
        )
        # The SSE stream can outrun the POST response. Completion must still be
        # matched to the compaction inbox id returned below.
        for event in _recorded_v2_events("events-v2-compact.sse"):
            sensor.on_raw_event(event)
        return 200, json.dumps(
            {
                "data": {
                    "id": "msg_v2_compaction",
                    "sessionID": V2_SESSION_ID,
                    "type": "compaction",
                    "payload": {},
                    "delivery": "steer",
                }
            }
        )

    sensor = opencode_idle.OpenCodeV2IdleSensor(
        _context(tmp_path, session_id=V2_SESSION_ID, version="v2"),
        request=request,
        compact_timeout_seconds=0.1,
    )
    iterator = sensor.events()

    assert (await sensor.compact()).result == "ok"
    assert [event.kind for event in await _take(iterator, 1)] == ["turn_end"]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(iterator), timeout=0.01)


@pytest.mark.parametrize(
    ("version", "expected_type"),
    [
        (None, opencode_idle.OpenCodeV2IdleSensor),
        ("v2", opencode_idle.OpenCodeV2IdleSensor),
        ("v1", opencode_idle.OpenCodeIdleSensor),
    ],
)
def test_primary_idle_sensor_dispatches_by_projected_version(
    tmp_path: Path,
    version: str | None,
    expected_type: type[object],
) -> None:
    sensor = opencode_idle.primary_idle_sensor(_context(tmp_path, version=version))

    assert isinstance(sensor, expected_type)
