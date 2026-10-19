from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness import opencode_idle, opencode_running_work
from meridian.lib.harness.connections.base import HarnessConnection, RawHarnessEvent
from meridian.lib.harness.connections.opencode_v2_http import OpenCodeV2Connection
from meridian.lib.harness.idle_types import BackgroundTask, CacheTtl, IdleEvent, IdleSensorContext
from meridian.lib.harness.permission_broker import PermissionTransition, _transition_to_event

FIXTURES = Path(__file__).parents[2] / "fixtures" / "opencode_idle"
SESSION_ID = "ses_ee21d9d97ffeN9m0VxoYy7qFQf"
V2_SESSION_ID = "ses_v2_fixture_primary"


class FakeConnection:
    observer_endpoint = None

    def __init__(self, resolved_version: str | None = None) -> None:
        self.resolved_version = resolved_version


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


def _v2_request_stub(*, provider: str, model_id: str) -> opencode_idle.BackendRequest:
    async def request(
        method: str, path: str, _payload: Mapping[str, object] | None
    ) -> tuple[int, str]:
        session_path = f"/api/session/{V2_SESSION_ID}"
        if (method, path) == ("GET", session_path):
            return 200, json.dumps(
                {"data": {"id": V2_SESSION_ID, "model": {"providerID": provider, "id": model_id}}}
            )
        assert (method, path) == ("GET", f"{session_path}/message?limit=20&order=desc")
        return 200, '{"data":[]}'

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


def test_v2_turn_end_parses_wrapped_descending_message_facts() -> None:
    payload = {
        "data": [
            {
                "info": {"role": "assistant"},
                "content": "latest answer",
            },
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
    assert await sensor.busy() is True
    sensor.on_raw_event(_v2_user_enqueued(delivered_id, "follow up after this turn"))
    sensor.on_raw_event(_v2_user_enqueued(cancelled_id, "never mind"))
    sensor.on_raw_event(_v2_raw("session.inbox.cancelled", inboxID=cancelled_id))
    sensor.on_raw_event(_v2_raw("session.execution.succeeded"))
    assert await sensor.busy() is False
    assert [event.kind for event in await _take(iterator, 3)] == ["user_prompt"] * 2 + ["turn_end"]
    sensor.on_raw_event(_v2_raw("session.execution.started"))
    sensor.on_raw_event(_v2_raw("session.inbox.delivered", inboxID=cancelled_id))
    sensor.on_raw_event(_v2_raw("session.inbox.delivered", inboxID=delivered_id))
    sensor.on_raw_event(_v2_raw("session.execution.succeeded"))
    assert [event.kind for event in await _take(iterator, 2)] == ["user_prompt", "turn_end"]
    await _assert_quiet(iterator)


_PROJECT_DIR = "/tmp/project"
_SHELL_LISTING_PATH = f"/api/shell?location%5Bdirectory%5D={_PROJECT_DIR.replace('/', '%2F')}"
_CHILD_LISTING_PATH = f"/api/session?parentID={V2_SESSION_ID}&limit=500"


def _v2_global(event_type: str, **payload: object) -> RawHarnessEvent:
    """An event the stream sends without a session id, such as ``shell.created``."""

    return RawHarnessEvent(
        event_type=event_type,
        payload={"type": event_type, **payload},
        harness_id="opencode",
    )


def _v2_turns(name: str) -> list[list[RawHarnessEvent]]:
    """A recorded stream cut after each primary terminal; the remainder is the last cut."""

    turns: list[list[RawHarnessEvent]] = [[]]
    for event in _recorded_v2_events(name):
        turns[-1].append(event)
        if (
            event.event_type in opencode_idle.TERMINAL_EVENTS
            and event.payload["sessionID"] == V2_SESSION_ID
        ):
            turns.append([])
    return turns


async def _turn_end(sensor: opencode_idle.OpenCodeV2IdleSensor, events: list[RawHarnessEvent]):
    for event in events:
        sensor.on_raw_event(event)
    iterator = sensor.events()
    while (idle := await asyncio.wait_for(anext(iterator), timeout=1)).kind != "turn_end":
        pass
    return idle


def _v2_work_request(
    *,
    shells: list[str] | None = None,
    children: list[dict[str, object]] | None = None,
    failing: str | None = None,
) -> opencode_idle.BackendRequest:
    async def request(
        method: str, path: str, _payload: Mapping[str, object] | None
    ) -> tuple[int, str]:
        assert method == "GET"
        if path == failing:
            return 500, ""
        session_path = f"/api/session/{V2_SESSION_ID}"
        if path == session_path:
            return 200, json.dumps(
                {"data": {"id": V2_SESSION_ID, "location": {"directory": _PROJECT_DIR}}}
            )
        if path == _SHELL_LISTING_PATH:
            return 200, json.dumps(
                {"data": [{"id": shell, "status": "running"} for shell in shells or []]}
            )
        if path == _CHILD_LISTING_PATH:
            return 200, json.dumps({"data": children or []})
        assert path.startswith(f"{session_path}/message")
        return 200, '{"data":[]}'

    return request


async def test_v2_background_shell_is_reported_at_turn_end_until_its_wake(tmp_path: Path) -> None:
    sensor, _ = _v2_sensor(tmp_path)
    started, woken, _ = _v2_turns("events-v2-background-shell.sse")

    assert (await _turn_end(sensor, started)).background_tasks == (
        BackgroundTask(kind="shell", command="sleep 15"),
    )
    assert (await _turn_end(sensor, woken)).background_tasks == ()


async def test_v2_background_subagent_is_reported_at_turn_end_until_its_wake(
    tmp_path: Path,
) -> None:
    sensor, _ = _v2_sensor(tmp_path)
    started, woken, _ = _v2_turns("events-v2-background-subagent.sse")

    # The child's own foreground shell is not the primary's work.
    assert (await _turn_end(sensor, started)).background_tasks == (BackgroundTask(kind="agent"),)
    assert (await _turn_end(sensor, woken)).background_tasks == ()


async def test_v2_work_no_tool_named_is_not_reported(tmp_path: Path) -> None:
    sensor, _ = _v2_sensor(tmp_path)
    announced = [
        event
        for event in _recorded_v2_events("events-v2-unreferenced-work.sse")
        if event.event_type in {"shell.created", "session.created"}
    ]

    # A session shell, a global shell, and a manual child session are all running.
    assert len(announced) == 3
    assert (
        await _turn_end(sensor, [*announced, _v2_raw("session.execution.succeeded")])
    ).background_tasks == ()


async def test_v2_interrupted_foreground_tools_are_not_reported(tmp_path: Path) -> None:
    sensor, _ = _v2_sensor(tmp_path)
    shell_turn, subagent_turn, *_ = _v2_turns("events-v2-interrupted-foreground.sse")

    # The stream ends the turn before it reports the aborted shell or child exiting.
    assert (await _turn_end(sensor, shell_turn)).background_tasks == ()
    assert (await _turn_end(sensor, subagent_turn)).background_tasks == ()


async def test_v2_reported_work_is_bounded_and_keeps_agents(tmp_path: Path) -> None:
    sensor, _ = _v2_sensor(tmp_path)
    events: list[RawHarnessEvent] = []
    for number, command in enumerate(["x" * 513, *("sleep 1" for _ in range(16))]):
        shell_id = f"sh_{number}"
        events.append(_v2_global("shell.created", info={"id": shell_id, "command": command}))
        events.append(_v2_raw("session.tool.progress", metadata={"shellID": shell_id}))
    # The child is named after seventeen shells, yet an agent is the certain hold.
    events.append(_v2_global("session.created", sessionID="ses_child", parentID=V2_SESSION_ID))
    events.append(
        _v2_raw("session.tool.progress", metadata={"sessionID": "ses_child", "status": "running"})
    )

    tasks = (
        await _turn_end(sensor, [*events, _v2_raw("session.execution.succeeded")])
    ).background_tasks

    assert len(tasks) == 16
    assert tasks[:2] == (BackgroundTask(kind="agent"), BackgroundTask(kind="other"))
    assert tasks[2:] == (BackgroundTask(kind="shell", command="sleep 1"),) * 14


_CHILD_ID = "ses_v2_fixture_child"


def _surfaced_request(
    status: str, request_id: str, session_id: str = _CHILD_ID
) -> RawHarnessEvent:
    """A permission request as the managed primary's broker surfaces it to the sensor."""

    transition = PermissionTransition(
        seq=1,
        request_id=request_id,
        request_type="approval",
        method="permission.asked",
        payload={"id": request_id, "sessionID": session_id, "action": "shell"},
        status=cast("Any", status),
        resolution={"decision": "approve", "reply": "once"} if status == "resolved" else None,
        error=None,
        timestamp="2026-10-19T00:00:00Z",
    )
    event = _transition_to_event(transition, harness_id="opencode")
    assert event is not None
    return event


async def test_v2_child_waiting_on_a_permission_prompt_stops_holding_the_push(
    tmp_path: Path,
) -> None:
    sensor, _ = _v2_sensor(tmp_path)
    started, woken, _ = _v2_turns("events-v2-background-subagent.sse")

    assert (await _turn_end(sensor, started)).background_tasks == (BackgroundTask(kind="agent"),)

    # The prompt arrives after the turn end armed the push with the child held.
    sensor.on_raw_event(_surfaced_request("pending", "per_1"))
    assert (await _turn_end(sensor, [])).background_tasks == ()

    # Answered: the child runs again and will wake the primary when it ends.
    sensor.on_raw_event(_surfaced_request("resolved", "per_1"))
    assert (await _turn_end(sensor, [])).background_tasks == (BackgroundTask(kind="agent"),)
    assert (await _turn_end(sensor, woken)).background_tasks == ()


async def test_v2_prompt_of_a_grandchild_stops_its_ancestor_child_counting(
    tmp_path: Path,
) -> None:
    sensor, _ = _v2_sensor(tmp_path)
    started, *_ = _v2_turns("events-v2-background-subagent.sse")
    await _turn_end(sensor, started)

    sensor.on_raw_event(
        _v2_global("session.created", sessionID="ses_grandchild", parentID=_CHILD_ID)
    )
    sensor.on_raw_event(_surfaced_request("pending", "per_deep", "ses_grandchild"))

    assert (await _turn_end(sensor, [])).background_tasks == ()


@pytest.mark.parametrize("closing", ["resolved", "failed", "cancelled"])
async def test_v2_every_way_a_prompt_closes_counts_the_child_again(
    tmp_path: Path, closing: str
) -> None:
    sensor, _ = _v2_sensor(tmp_path)
    started, *_ = _v2_turns("events-v2-background-subagent.sse")
    await _turn_end(sensor, started)
    sensor.on_raw_event(_surfaced_request("pending", "per_1"))
    assert (await _turn_end(sensor, [])).background_tasks == ()

    sensor.on_raw_event(_surfaced_request(closing, "per_1"))

    assert (await _turn_end(sensor, [])).background_tasks == (BackgroundTask(kind="agent"),)


async def test_v2_prompts_that_change_no_reported_work_do_not_rearm(tmp_path: Path) -> None:
    sensor, _ = _v2_sensor(tmp_path)
    started, *_ = _v2_turns("events-v2-background-subagent.sse")
    await _turn_end(sensor, started)

    sensor.on_raw_event(_surfaced_request("pending", "per_primary", V2_SESSION_ID))
    sensor.on_raw_event(_surfaced_request("pending", "per_unknown", "ses_elsewhere"))
    sensor.on_raw_event(_surfaced_request("resolved", "per_never_opened"))
    # A busy primary reports the corrected work at its own turn end.
    sensor.on_raw_event(_v2_raw("session.execution.started"))
    sensor.on_raw_event(_surfaced_request("pending", "per_while_busy"))
    ended = await _turn_end(sensor, [_v2_raw("session.execution.succeeded", id="evt_own_end")])

    assert (ended.turn_id, ended.background_tasks) == ("evt_own_end", ())


async def test_v2_prompt_closed_after_its_child_ended_changes_nothing(tmp_path: Path) -> None:
    sensor, _ = _v2_sensor(tmp_path)
    started, *_ = _v2_turns("events-v2-background-subagent.sse")
    await _turn_end(sensor, started)
    sensor.on_raw_event(_surfaced_request("pending", "per_1"))
    await _turn_end(sensor, [])

    sensor.on_raw_event(_v2_raw("session.execution.succeeded", sessionID=_CHILD_ID))
    sensor.on_raw_event(_surfaced_request("resolved", "per_1"))
    ended = await _turn_end(sensor, [_v2_raw("session.execution.succeeded", id="evt_own_end")])

    assert (ended.turn_id, ended.background_tasks) == ("evt_own_end", ())


@pytest.mark.parametrize(
    ("request_kwargs", "expected_after"),
    [
        ({"shells": ["sh_152d5c83a0014ffNOwmGPo8RO7"]}, None),
        ({"shells": []}, ()),
        ({"failing": _SHELL_LISTING_PATH}, ()),
    ],
)
async def test_v2_reconnect_rederives_work_at_once_and_rearms_if_it_changed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    request_kwargs: dict[str, Any],
    expected_after: tuple[BackgroundTask, ...] | None,
) -> None:
    sensor, _ = _v2_sensor(tmp_path, request=_v2_work_request(**request_kwargs))
    started, *_ = _v2_turns("events-v2-background-shell.sse")
    shell = (BackgroundTask(kind="shell", command="sleep 15"),)
    assert (await _turn_end(sensor, started)).background_tasks == shell

    # The stream reopened while the shell ran: its end may have fallen in the gap,
    # and no later turn end is coming to find out.
    with caplog.at_level(logging.WARNING, logger=opencode_running_work.logger.name):
        sensor.on_raw_event(_v2_global("server.connected"))
        if expected_after is not None:
            assert (await _turn_end(sensor, [])).background_tasks == expected_after
        else:
            await asyncio.sleep(0.05)
            ended = await _turn_end(
                sensor, [_v2_raw("session.execution.succeeded", id="evt_own_end")]
            )
            assert (ended.turn_id, ended.background_tasks) == ("evt_own_end", shell)

    assert [
        record.getMessage().startswith("OpenCode running work") for record in caplog.records
    ] == ([True] if "failing" in request_kwargs else [])


async def test_v2_reconnect_drops_a_child_that_ended_in_the_gap(tmp_path: Path) -> None:
    child = {"id": _CHILD_ID, "outcome": "succeeded"}
    sensor, _ = _v2_sensor(tmp_path, request=_v2_work_request(children=[child]))
    started, *_ = _v2_turns("events-v2-background-subagent.sse")
    assert (await _turn_end(sensor, started)).background_tasks == (BackgroundTask(kind="agent"),)

    sensor.on_raw_event(_v2_global("server.connected"))

    assert (await _turn_end(sensor, [])).background_tasks == ()


async def test_v2_first_connection_with_no_work_named_asks_the_server_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A listing that fails would log a warning if the sensor asked for it.
    sensor, _ = _v2_sensor(tmp_path, request=_v2_work_request(failing=_SHELL_LISTING_PATH))

    with caplog.at_level(logging.WARNING, logger=opencode_running_work.logger.name):
        sensor.on_raw_event(_v2_global("server.connected"))
        await asyncio.sleep(0.05)
        ended = await _turn_end(sensor, [_v2_raw("session.execution.succeeded", id="evt_own_end")])

    assert (ended.turn_id, ended.background_tasks) == ("evt_own_end", ())
    assert caplog.records == []


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
