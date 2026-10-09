from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness import opencode_idle
from meridian.lib.harness.bundle import get_harness_bundle
from meridian.lib.harness.connections.base import HarnessConnection, RawHarnessEvent
from meridian.lib.harness.idle_types import IdleEnvFacts, IdleEvent, IdleSensorContext

FIXTURES = Path(__file__).parents[2] / "fixtures" / "opencode_idle"
SESSION_ID = "ses_ee21d9d97ffeN9m0VxoYy7qFQf"


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
    alive: Callable[[], bool] = lambda: True,
    tmux_pane: str | None = "%7",
) -> IdleSensorContext:
    return IdleSensorContext(
        connection=cast("HarnessConnection[Any]", FakeConnection()),
        harness_id=HarnessId.OPENCODE,
        harness_session_id=SESSION_ID,
        env={},
        tmux_pane=tmux_pane,
        tui_alive=alive,
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


async def _take(iterator: AsyncIterator[IdleEvent], count: int) -> list[IdleEvent]:
    return [await anext(iterator) for _ in range(count)]


async def test_recorded_normal_prompt_emits_one_return_and_one_turn_end(
    tmp_path: Path,
) -> None:
    clock = FakeClock(1_791_502_764.0)
    sensor = opencode_idle.OpenCodeIdleSensor(_context(tmp_path), now=clock)
    iterator = sensor.events()

    for event in _recorded_events("events-normal.sse"):
        sensor.on_raw_event(event)

    observed = await _take(iterator, 3)
    assert [event.kind for event in observed] == ["user_prompt", "busy", "turn_end"]
    assert sum(event.kind == "user_prompt" for event in observed) == 1
    assert sum(event.kind == "turn_end" for event in observed) == 1
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(iterator), timeout=0.01)


async def test_recorded_summarize_never_emits_user_return(tmp_path: Path) -> None:
    sensor = opencode_idle.OpenCodeIdleSensor(
        _context(tmp_path),
        now=FakeClock(1_791_502_786.0),
    )
    iterator = sensor.events()

    for event in _recorded_events("events-summarize.sse"):
        sensor.on_raw_event(event)

    observed = await _take(iterator, 2)
    assert [event.kind for event in observed] == ["busy", "turn_end"]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(iterator), timeout=0.01)


async def test_startup_idle_is_ignored_until_busy(tmp_path: Path) -> None:
    sensor = opencode_idle.OpenCodeIdleSensor(_context(tmp_path), now=FakeClock(10.0))
    iterator = sensor.events()
    sensor.on_raw_event(
        RawHarnessEvent(
            event_type="session.status",
            payload={
                "type": "session.status",
                "properties": {"sessionID": SESSION_ID, "status": {"type": "idle"}},
            },
            harness_id="opencode",
        )
    )
    sensor.on_raw_event(
        RawHarnessEvent(
            event_type="session.idle",
            payload={"type": "session.idle", "properties": {"sessionID": SESSION_ID}},
            harness_id="opencode",
        )
    )

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(iterator), timeout=0.01)


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [("pane-draft-typed.txt", "yes"), ("pane-draft-empty.txt", "no")],
)
async def test_facts_read_draft_from_recorded_pane(
    tmp_path: Path,
    fixture: str,
    expected: str,
) -> None:
    async def capture(_pane: str) -> str | None:
        return (FIXTURES / fixture).read_text(encoding="utf-8")

    sensor = opencode_idle.OpenCodeIdleSensor(_context(tmp_path), capture_pane=capture)

    facts = await sensor.facts()

    assert facts.draft == expected
    assert facts.busy is False
    assert facts.agents_running == 0
    assert facts.context_tokens is None


async def test_facts_fail_closed_without_readable_pane(tmp_path: Path) -> None:
    async def unreadable(_pane: str) -> str | None:
        return None

    sensor = opencode_idle.OpenCodeIdleSensor(
        _context(tmp_path, tmux_pane=None),
        capture_pane=unreadable,
    )

    assert (await sensor.facts()).draft == "unknown"


@pytest.mark.parametrize(
    ("summarize_status", "summarize_body", "expected_result", "expected_reason"),
    [
        (200, "true", "ok", None),
        (500, '{"name":"UnknownError"}', "failed", '{"name":"UnknownError"}'),
    ],
)
async def test_compact_uses_current_session_model_and_maps_response(
    tmp_path: Path,
    summarize_status: int,
    summarize_body: str,
    expected_result: str,
    expected_reason: str | None,
) -> None:
    requests: list[tuple[str, str, Mapping[str, object] | None]] = []

    async def request(
        method: str,
        path: str,
        payload: Mapping[str, object] | None,
    ) -> tuple[int, str]:
        requests.append((method, path, payload))
        if method == "GET":
            return 200, '{"model":{"providerID":"opencode","id":"big-pickle"}}'
        return summarize_status, summarize_body

    sensor = opencode_idle.OpenCodeIdleSensor(_context(tmp_path), request=request)

    result = await sensor.compact()

    assert result.result == expected_result
    assert result.reason == expected_reason
    assert requests == [
        ("GET", f"/session/{SESSION_ID}", None),
        (
            "POST",
            f"/session/{SESSION_ID}/summarize",
            {"providerID": "opencode", "modelID": "big-pickle"},
        ),
    ]


async def test_compact_does_not_act_after_tui_exit(tmp_path: Path) -> None:
    requested = False

    async def request(
        _method: str,
        _path: str,
        _payload: Mapping[str, object] | None,
    ) -> tuple[int, str]:
        nonlocal requested
        requested = True
        return 200, "true"

    sensor = opencode_idle.OpenCodeIdleSensor(
        _context(tmp_path, alive=lambda: False),
        request=request,
    )

    result = await sensor.compact()

    assert result.result == "vetoed"
    assert result.reason == "tui-exited"
    assert requested is False


def test_opencode_bundle_registers_idle_sensor_and_environment_facts() -> None:
    bundle = get_harness_bundle(HarnessId.OPENCODE)
    facts = bundle.idle_env_facts

    assert bundle.primary_idle_sensor is opencode_idle.primary_idle_sensor
    assert bundle.detect_ttl is None
    assert facts is opencode_idle.idle_env_facts
    assert facts is not None
    assert facts({"OPENCODE_DISABLE_AUTOCOMPACT": "set"}) == IdleEnvFacts(
        harness_autocompact_off=True, cache_retention=None
    )
    assert facts({"OPENCODE_DISABLE_AUTOCOMPACT": ""}) == IdleEnvFacts(
        harness_autocompact_off=False, cache_retention=None
    )
