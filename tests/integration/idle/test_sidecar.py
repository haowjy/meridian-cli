from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from meridian.lib.config.settings import MeridianConfig
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.connections.base import HarnessConnection
from meridian.lib.harness.idle_types import (
    CompactResult,
    IdleEvent,
    IdleFacts,
    IdleSensorContext,
)
from meridian.lib.idle.service import IdleService, NoticeSpec, NotifyReport
from meridian.lib.idle.sidecar import SidecarClock, run
from meridian.lib.state.idle_store import IdleStore


class Clock:
    def __init__(self) -> None:
        self.value = 0

    def now_ms(self) -> int:
        return self.value

    async def sleep_until_ms(self, deadline_ms: int) -> None:
        self.value = max(self.value, deadline_ms)
        await asyncio.sleep(0)


@dataclass(frozen=True)
class Report:
    ok: bool = True


class Sender:
    def __init__(self) -> None:
        self.notices: list[NoticeSpec] = []

    def send(self, notice: NoticeSpec, cfg: object) -> NotifyReport:
        _ = cfg
        self.notices.append(notice)
        return Report()


class Sensor:
    def __init__(self, events: tuple[IdleEvent, ...], alive: list[bool]) -> None:
        self._events = events
        self._alive = alive
        self.compact_calls = 0

    def on_raw_event(self, event: object) -> None:
        _ = event

    async def events(self) -> AsyncIterator[IdleEvent]:
        for event in self._events:
            yield event
            await asyncio.sleep(0)
        while self._alive[0]:
            await asyncio.sleep(0.001)

    async def facts(self) -> IdleFacts:
        return IdleFacts("no", False, 0, 100_000, False)

    async def compact(self) -> CompactResult:
        self.compact_calls += 1
        self._alive[0] = False
        return CompactResult("ok", "100k → summary")


class RaisingSensor(Sensor):
    async def events(self) -> AsyncIterator[IdleEvent]:
        if False:
            yield IdleEvent("idle", "unused", None, 0)
        raise SensorFailure("broken stream")


class SensorFailure(BaseException):
    pass


def context(tmp_path: Path, alive: list[bool]) -> IdleSensorContext:
    return IdleSensorContext(
        connection=cast("HarnessConnection[Any]", object()),
        harness_id=HarnessId.CODEX,
        harness_session_id="session-1",
        env={"MERIDIAN_SESSION_ROLE": "primary"},
        tmux_pane=None,
        tui_alive=lambda: alive[0],
        spawn_dir=tmp_path / "p1",
    )


def policy(
    tmp_path: Path,
    clock: Clock,
    sender: Sender,
) -> tuple[IdleService, IdleStore]:
    store = IdleStore(tmp_path / "idle", now_ms=clock.now_ms)
    return (
        IdleService(
            store=store,
            config=MeridianConfig(),
            env={"MERIDIAN_SESSION_ROLE": "primary"},
            now_ms=clock.now_ms,
            notify_sender=sender,
            spawn_reader=lambda: (),
        ),
        store,
    )


@pytest.mark.asyncio
async def test_sidecar_drives_idle_push_warn_compact_and_done(tmp_path: Path) -> None:
    alive = [True]
    clock = Clock()
    sender = Sender()
    service, store = policy(tmp_path, clock, sender)
    sensor = Sensor((IdleEvent("turn_end", "session-1", "turn-1", 0),), alive)

    await run(
        sensor,
        context(tmp_path, alive),
        service=service,
        clock=cast("SidecarClock", clock),
        poll_seconds=0.001,
    )

    state = store.read("codex", "session-1")
    assert [notice.body for notice in sender.notices] == [
        "waiting on you",
        "cache cold in 15m",
        "compacted (100k → summary)",
    ]
    assert sensor.compact_calls == 1
    assert state is not None
    assert state.done == {"push": "sent", "warn": "sent", "compact": "ok"}


@pytest.mark.asyncio
async def test_sidecar_user_return_cancels_pending_schedule(tmp_path: Path) -> None:
    alive = [True]
    clock = Clock()
    sender = Sender()
    service, store = policy(tmp_path, clock, sender)
    events = (
        IdleEvent("turn_end", "session-1", "turn-1", 0),
        IdleEvent("user_prompt", "session-1", None, 1),
    )
    sensor = Sensor(events, alive)

    task = asyncio.create_task(
        run(
            sensor,
            context(tmp_path, alive),
            service=service,
            clock=cast("SidecarClock", clock),
            poll_seconds=0.001,
        )
    )
    await asyncio.sleep(0.005)
    alive[0] = False
    await task

    state = store.read("codex", "session-1")
    assert sender.notices == []
    assert sensor.compact_calls == 0
    assert state is not None and state.stretch_open is False


@pytest.mark.asyncio
async def test_sidecar_contains_base_exception_from_sensor(tmp_path: Path) -> None:
    alive = [True]
    clock = Clock()
    sender = Sender()
    service, _ = policy(tmp_path, clock, sender)

    await run(
        RaisingSensor((), alive),
        context(tmp_path, alive),
        service=service,
        clock=cast("SidecarClock", clock),
        poll_seconds=0.001,
    )

    debug_path = tmp_path / "p1" / "debug.jsonl"
    assert "SensorFailure" in debug_path.read_text(encoding="utf-8")
