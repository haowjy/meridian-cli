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
from meridian.lib.harness.idle_types import CacheTtl, IdleEvent, IdleSensorContext

FIXTURES = Path(__file__).parents[2] / "fixtures" / "opencode_idle"
V2_SESSION_ID = "ses_v2_fixture_primary"


class FakeConnection:
    observer_endpoint = None

    def __init__(self, resolved_version: str | None = None) -> None:
        self.resolved_version = resolved_version


def _context(
    tmp_path: Path,
    *,
    session_id: str = V2_SESSION_ID,
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

    facts = await opencode_idle.OpenCodeV2IdleSensor(
        _context(tmp_path, version="v2"), capture_pane=capture
    ).facts()

    assert (facts.draft, facts.busy) == (expected_draft, False)


async def test_v2_recorded_normal_prompt_emits_return_and_parent_turn_end(
    tmp_path: Path,
) -> None:
    async def request(
        method: str,
        path: str,
        _payload: Mapping[str, object] | None,
    ) -> tuple[int, str]:
        assert method == "GET"
        if path == f"/api/session/{V2_SESSION_ID}":
            return 200, json.dumps(
                {
                    "data": {
                        "id": V2_SESSION_ID,
                        "model": {"providerID": "openai", "id": "gpt-6-astra"},
                    }
                }
            )
        assert path == f"/api/session/{V2_SESSION_ID}/message?limit=20&order=desc"
        return 200, '{"data":[],"cursor":{"previous":null,"next":null}}'

    sensor = opencode_idle.OpenCodeV2IdleSensor(
        _context(tmp_path, session_id=V2_SESSION_ID, version="v2"),
        request=request,
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

    observed = await _take(iterator, 3)
    assert [event.kind for event in observed] == [
        "user_prompt",
        "user_prompt",
        "turn_end",
    ]
    assert observed[2].turn_id == "evt_fixture_26"
    assert observed[2].provider == "openai"
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
async def test_v2_all_parent_execution_outcomes_end_the_turn(
    tmp_path: Path,
    terminal_type: str,
) -> None:
    sensor = opencode_idle.OpenCodeV2IdleSensor(
        _context(tmp_path, session_id=V2_SESSION_ID, version="v2")
    )
    iterator = sensor.events()
    terminal = _recorded_v2_events("events-v2-normal.sse")[-1]

    sensor.on_raw_event(
        RawHarnessEvent(
            event_type=terminal_type,
            payload={**terminal.payload, "type": terminal_type},
            harness_id=terminal.harness_id,
            raw_text=terminal.raw_text,
        )
    )

    assert (await anext(iterator)).kind == "turn_end"


async def test_v2_turn_end_fetches_wrapped_descending_message_excerpts(
    tmp_path: Path,
) -> None:
    async def request(
        method: str,
        path: str,
        _payload: Mapping[str, object] | None,
    ) -> tuple[int, str]:
        assert method == "GET"
        if path == f"/api/session/{V2_SESSION_ID}":
            return 200, json.dumps(
                {
                    "data": {
                        "id": V2_SESSION_ID,
                        "model": {"providerID": "openai", "id": "gpt-6-astra"},
                    }
                }
            )
        assert path == f"/api/session/{V2_SESSION_ID}/message?limit=20&order=desc"
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

    turn_end = (await _take(iterator, 3))[-1]
    assert turn_end.last_user_text == "latest"
    assert turn_end.last_assistant_text == "answer one answer two"
    assert turn_end.provider == "openai"


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


async def test_v2_steered_prompt_returns_before_terminal_and_does_not_leak_to_compaction(
    tmp_path: Path,
) -> None:
    sensor = opencode_idle.OpenCodeV2IdleSensor(
        _context(tmp_path, session_id=V2_SESSION_ID, version="v2")
    )
    iterator = sensor.events()
    normal = _recorded_v2_events("events-v2-normal.sse")
    steered = RawHarnessEvent(
        event_type="session.inbox.enqueued",
        payload={
            "type": "session.inbox.enqueued",
            "sessionID": V2_SESSION_ID,
            "inboxID": "msg_v2_user_steered",
            "item": {
                "type": "user",
                "payload": {"text": "follow up while the turn is running"},
                "delivery": "steer",
            },
        },
        harness_id="opencode",
    )

    for event in (*normal[:2], steered, *normal[2:]):
        sensor.on_raw_event(event)
    for event in _recorded_v2_events("events-v2-compact.sse"):
        sensor.on_raw_event(event)

    observed = await _take(iterator, 5)
    assert [event.kind for event in observed] == [
        "user_prompt",
        "user_prompt",
        "user_prompt",
        "turn_end",
        "turn_end",
    ]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(iterator), timeout=0.01)


async def test_v2_queued_prompt_returns_when_delivered_for_second_execution(
    tmp_path: Path,
) -> None:
    sensor = opencode_idle.OpenCodeV2IdleSensor(
        _context(tmp_path, session_id=V2_SESSION_ID, version="v2")
    )
    iterator = sensor.events()

    def raw(event_type: str, **payload: object) -> RawHarnessEvent:
        return RawHarnessEvent(
            event_type=event_type,
            payload={"type": event_type, "sessionID": V2_SESSION_ID, **payload},
            harness_id="opencode",
        )

    sensor.on_raw_event(raw("session.execution.started"))
    sensor.on_raw_event(
        raw(
            "session.inbox.enqueued",
            inboxID="msg_v2_user_queued",
            item={
                "type": "user",
                "payload": {"text": "follow up after this turn"},
                "delivery": "queue",
            },
        )
    )
    sensor.on_raw_event(raw("session.execution.succeeded"))
    sensor.on_raw_event(raw("session.execution.started"))
    sensor.on_raw_event(
        raw("session.inbox.delivered", inboxID="msg_v2_user_queued")
    )
    sensor.on_raw_event(raw("session.execution.succeeded"))

    observed = await asyncio.wait_for(_take(iterator, 4), timeout=0.1)
    assert [event.kind for event in observed] == [
        "user_prompt",
        "turn_end",
        "user_prompt",
        "turn_end",
    ]


async def test_v2_cancelled_prompt_is_no_longer_pending_or_returned(
    tmp_path: Path,
) -> None:
    sensor = opencode_idle.OpenCodeV2IdleSensor(
        _context(tmp_path, session_id=V2_SESSION_ID, version="v2")
    )
    iterator = sensor.events()

    def raw(event_type: str, **payload: object) -> RawHarnessEvent:
        return RawHarnessEvent(
            event_type=event_type,
            payload={"type": event_type, "sessionID": V2_SESSION_ID, **payload},
            harness_id="opencode",
        )

    sensor.on_raw_event(
        raw(
            "session.inbox.enqueued",
            inboxID="msg_v2_user_cancelled",
            item={
                "type": "user",
                "payload": {"text": "never mind"},
                "delivery": "queue",
            },
        )
    )
    sensor.on_raw_event(
        raw("session.inbox.cancelled", inboxID="msg_v2_user_cancelled")
    )
    assert sensor._pending_user_inbox_ids == set()

    sensor.on_raw_event(
        raw("session.inbox.delivered", inboxID="msg_v2_user_cancelled")
    )

    assert (await anext(iterator)).kind == "user_prompt"
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(iterator), timeout=0.01)


async def test_v2_stranded_steer_delivery_during_compaction_does_not_emit_return(
    tmp_path: Path,
) -> None:
    sensor: opencode_idle.OpenCodeV2IdleSensor

    def raw(event_type: str, **payload: object) -> RawHarnessEvent:
        return RawHarnessEvent(
            event_type=event_type,
            payload={"type": event_type, "sessionID": V2_SESSION_ID, **payload},
            harness_id="opencode",
        )

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
                        "model": {"providerID": "openai", "id": "gpt-6-luna"},
                    }
                }
            )
        assert (path, payload) == (f"/api/session/{V2_SESSION_ID}/compact", {})
        sensor.on_raw_event(raw("session.execution.started"))
        sensor.on_raw_event(
            raw("session.compaction.started", inputID="msg_v2_compaction")
        )
        sensor.on_raw_event(
            raw("session.inbox.delivered", inboxID="msg_v2_user_stranded")
        )
        sensor.on_raw_event(raw("session.compaction.ended"))
        sensor.on_raw_event(raw("session.execution.succeeded"))
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

    sensor.on_raw_event(raw("session.execution.started"))
    sensor.on_raw_event(
        raw(
            "session.inbox.enqueued",
            inboxID="msg_v2_user_stranded",
            item={
                "type": "user",
                "payload": {"text": "follow up while this turn runs"},
                "delivery": "steer",
            },
        )
    )
    sensor.on_raw_event(raw("session.execution.succeeded"))
    assert [event.kind for event in await _take(iterator, 2)] == [
        "user_prompt",
        "turn_end",
    ]

    result = await sensor.compact()

    assert result.result == "ok"
    assert (await anext(iterator)).kind == "turn_end"
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
    ("terminal_type", "include_ended"),
    [
        ("session.execution.failed", False),
        ("session.execution.interrupted", False),
        ("session.execution.failed", True),
    ],
)
async def test_v2_compact_resolves_terminal_failure_immediately(
    tmp_path: Path,
    terminal_type: str,
    include_ended: bool,
) -> None:
    sensor: opencode_idle.OpenCodeV2IdleSensor

    async def request(
        method: str,
        path: str,
        payload: Mapping[str, object] | None,
    ) -> tuple[int, str]:
        if method == "GET":
            assert path == f"/api/session/{V2_SESSION_ID}"
            return 200, json.dumps(
                {
                    "data": {
                        "id": V2_SESSION_ID,
                        "model": {"providerID": "openai", "id": "gpt-6-luna"},
                    }
                }
            )
        assert (method, path, payload) == (
            "POST",
            f"/api/session/{V2_SESSION_ID}/compact",
            {},
        )
        for event in _recorded_v2_events("events-v2-compact.sse"):
            if event.event_type == "session.compaction.ended" and not include_ended:
                continue
            if event.event_type in {
                "session.execution.succeeded",
                "session.execution.failed",
                "session.execution.interrupted",
            }:
                event = RawHarnessEvent(
                    event_type=terminal_type,
                    payload={**event.payload, "type": terminal_type},
                    harness_id=event.harness_id,
                    raw_text=event.raw_text,
                )
            sensor.on_raw_event(event)
        return 200, json.dumps(
            {
                "data": {
                    "id": "msg_v2_compaction",
                    "sessionID": V2_SESSION_ID,
                    "type": "compaction",
                }
            }
        )

    sensor = opencode_idle.OpenCodeV2IdleSensor(
        _context(tmp_path, session_id=V2_SESSION_ID, version="v2"),
        request=request,
        compact_timeout_seconds=10,
    )

    result = await asyncio.wait_for(sensor.compact(), timeout=0.1)

    assert result.result == "failed"
    assert result.reason == terminal_type


@pytest.mark.parametrize(
    ("resolved_version", "expected_type"),
    [
        ("v1", None),
        ("v2", opencode_idle.OpenCodeV2IdleSensor),
        (None, opencode_idle.OpenCodeV2IdleSensor),
    ],
)
def test_primary_idle_sensor_dispatches_by_resolved_connection_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    resolved_version: str | None,
    expected_type: type[object] | None,
) -> None:
    def resolve(
        _preference: str | None,
        *,
        binary: str,
        env: Mapping[str, str],
    ) -> str:
        assert resolved_version is None
        assert (binary, env) == ("opencode", {})
        return "v2"

    monkeypatch.setattr(opencode_idle, "resolve_opencode_version", resolve)
    sensor = opencode_idle.primary_idle_sensor(
        _context(tmp_path, resolved_version=resolved_version)
    )

    assert sensor is None if expected_type is None else type(sensor) is expected_type
    expected_warnings = (
        ["OpenCode 1.x idle integration is unsupported; upgrade to OpenCode 2.x"]
        if resolved_version == "v1"
        else []
    )
    assert caplog.messages == expected_warnings


def test_primary_idle_sensor_resolves_preference_when_connection_cannot_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    observed: list[tuple[str | None, Mapping[str, str]]] = []

    def resolve(
        preference: str | None,
        *,
        binary: str,
        env: Mapping[str, str],
    ) -> str:
        assert binary == "opencode"
        observed.append((preference, env))
        return "v1"

    monkeypatch.setattr(opencode_idle, "resolve_opencode_version", resolve, raising=False)

    sensor = opencode_idle.primary_idle_sensor(_context(tmp_path))

    assert sensor is None
    assert observed == [(None, {})]
    assert caplog.messages == [
        "OpenCode 1.x idle integration is unsupported; upgrade to OpenCode 2.x"
    ]


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("openai", CacheTtl(1800, estimated=True)),
        ("opencode-go", None),
    ],
)
def test_detect_ttl_by_provider(
    provider: str,
    expected: CacheTtl | None,
) -> None:
    assert (
        opencode_idle.detect_ttl(
            session_id=V2_SESSION_ID,
            native_store=None,
            provider=provider,
            env={},
        )
        == expected
    )
