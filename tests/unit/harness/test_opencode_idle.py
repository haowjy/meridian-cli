from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness import opencode_idle
from meridian.lib.harness.connections.base import HarnessConnection, RawHarnessEvent
from meridian.lib.harness.idle_types import IdleEvent, IdleSensorContext

FIXTURES = Path(__file__).parents[2] / "fixtures" / "opencode_idle"
SESSION_ID = "ses_ee21d9d97ffeN9m0VxoYy7qFQf"


class FakeConnection:
    observer_endpoint = None


def _context(tmp_path: Path) -> IdleSensorContext:
    return IdleSensorContext(
        connection=cast("HarnessConnection[Any]", FakeConnection()),
        harness_id=HarnessId.OPENCODE,
        harness_session_id=SESSION_ID,
        env={},
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


async def test_recorded_summarize_never_emits_user_return(tmp_path: Path) -> None:
    sensor = opencode_idle.OpenCodeIdleSensor(_context(tmp_path), now=lambda: 1_791_502_786.0)
    iterator = sensor.events()

    for event in _recorded_events("events-summarize.sse"):
        sensor.on_raw_event(event)

    assert [event.kind for event in await _take(iterator, 1)] == ["turn_end"]
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
