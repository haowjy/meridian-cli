from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from meridian.lib.config.settings import MeridianConfig
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import HarnessConnection
from meridian.lib.harness.idle_types import (
    CompactResult,
    IdleEvent,
    IdleFacts,
    IdleSensorContext,
)
from meridian.lib.idle.service import IdleService
from meridian.lib.idle.sidecar import SidecarClock, run
from meridian.lib.notify import Notice, SendReport
from meridian.lib.notify.channels.base import SendResult
from meridian.lib.state.idle_store import IdleStore
from tests.support.async_determinism import wait_until


class Clock:
    def __init__(self) -> None:
        self.value = 0

    def now_ms(self) -> int:
        return self.value

    async def sleep_until_ms(self, deadline_ms: int) -> None:
        self.value = max(self.value, deadline_ms)
        await asyncio.sleep(0)


class PendingClock(Clock):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def sleep_until_ms(self, deadline_ms: int) -> None:
        self.value = max(self.value, deadline_ms)
        await self.release.wait()


class Sender:
    def __init__(self) -> None:
        self.notices: list[Notice] = []

    def __call__(self, notice: Notice, cfg: object) -> SendReport:
        _ = cfg
        self.notices.append(notice)
        return SendReport(results=(SendResult(channel="test", status="sent"),))


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
        return CompactResult("ok", "100k → summary")


class RaisingSensor(Sensor):
    async def events(self) -> AsyncIterator[IdleEvent]:
        if False:
            yield
        raise SensorFailure("broken stream")


class BlockingCompactionSensor(Sensor):
    def __init__(self, alive: list[bool]) -> None:
        super().__init__((), alive)
        self.compact_started = asyncio.Event()
        self.release_compact = asyncio.Event()
        self.compact_cancelled = False

    async def events(self) -> AsyncIterator[IdleEvent]:
        yield IdleEvent("turn_end", "session-1", "turn-1")
        await self.compact_started.wait()
        yield IdleEvent("user_prompt", "session-1", None)
        while self._alive[0]:
            await asyncio.sleep(0.001)

    async def compact(self) -> CompactResult:
        self.compact_calls += 1
        self.compact_started.set()
        try:
            await self.release_compact.wait()
        except asyncio.CancelledError:
            self.compact_cancelled = True
            raise
        return CompactResult("ok", "100k → summary")


class ExternalSensor(Sensor):
    external_events = True


class FailingOnceService:
    def __init__(self, delegate: IdleService) -> None:
        self.delegate = delegate
        self.failed = False

    def event(self, *args: Any, **kwargs: Any) -> Any:
        if not self.failed:
            self.failed = True
            raise OSError("transient store failure")
        return self.delegate.event(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)


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
        spawn_id=SpawnId("p1"),
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
    sensor = Sensor((IdleEvent("turn_end", "session-1", "turn-1"),), alive)

    task = asyncio.create_task(
        run(
            sensor,
            context(tmp_path, alive),
            service=service,
            clock=cast("SidecarClock", clock),
            poll_seconds=0.001,
        )
    )
    await wait_until(
        lambda: bool(
            (state := store.read("codex", "session-1")) is not None
            and state.done.get("compact") == "ok"
        ),
        description="compaction completion",
    )
    await wait_until(
        lambda: bool(sender.notices and sender.notices[-1].body.startswith("compacted")),
        description="compaction report",
    )
    alive[0] = False
    await task

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
    clock = PendingClock()
    sender = Sender()
    service, store = policy(tmp_path, clock, sender)
    events = (
        IdleEvent("turn_end", "session-1", "turn-1"),
        IdleEvent("user_prompt", "session-1", None),
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
    await wait_until(
        lambda: bool(
            (state := store.read("codex", "session-1")) is not None and not state.stretch_open
        ),
        description="user return",
    )
    alive[0] = False
    await task

    state = store.read("codex", "session-1")
    assert sender.notices == []
    assert sensor.compact_calls == 0
    assert state is not None and state.stretch_open is False


@pytest.mark.asyncio
async def test_sidecar_user_return_does_not_cancel_inflight_compaction(tmp_path: Path) -> None:
    alive = [True]
    clock = Clock()
    sender = Sender()
    service, store = policy(tmp_path, clock, sender)
    sensor = BlockingCompactionSensor(alive)

    task = asyncio.create_task(
        run(
            sensor,
            context(tmp_path, alive),
            service=service,
            clock=cast("SidecarClock", clock),
            poll_seconds=0.001,
        )
    )
    await wait_until(sensor.compact_started.is_set, description="compaction start")
    await wait_until(
        lambda: bool(
            (state := store.read("codex", "session-1")) is not None and not state.stretch_open
        ),
        description="user return",
    )

    assert sensor.compact_cancelled is False
    sensor.release_compact.set()
    await wait_until(
        lambda: bool(
            (state := store.read("codex", "session-1")) is not None
            and state.done.get("compact") == "ok"
        ),
        description="compaction completion",
    )
    await wait_until(
        lambda: bool(sender.notices and sender.notices[-1].body.startswith("compacted")),
        description="compaction report",
    )
    alive[0] = False
    await task

    state = store.read("codex", "session-1")
    assert state is not None
    assert state.stretch_open is False
    assert state.done["compact"] == "ok"
    assert sender.notices[-1].body == "compacted (100k → summary)"


@pytest.mark.asyncio
async def test_sidecar_teardown_cancels_compaction_and_records_failure(tmp_path: Path) -> None:
    alive = [True]
    clock = Clock()
    sender = Sender()
    service, store = policy(tmp_path, clock, sender)
    sensor = BlockingCompactionSensor(alive)

    task = asyncio.create_task(
        run(
            sensor,
            context(tmp_path, alive),
            service=service,
            clock=cast("SidecarClock", clock),
            poll_seconds=0.001,
        )
    )
    await wait_until(sensor.compact_started.is_set, description="compaction start")
    alive[0] = False
    await task

    state = store.read("codex", "session-1")
    assert sensor.compact_cancelled is True
    assert state is not None
    assert state.done["compact"] == "failed"
    assert sender.notices[-1].body == "compaction failed (teardown)"


@pytest.mark.asyncio
async def test_external_sensor_recovers_an_externally_written_arm(tmp_path: Path) -> None:
    alive = [True]
    clock = Clock()
    sender = Sender()
    service, store = policy(tmp_path, clock, sender)
    sensor = ExternalSensor((), alive)
    task = asyncio.create_task(
        run(
            sensor,
            context(tmp_path, alive),
            service=service,
            clock=cast("SidecarClock", clock),
            poll_seconds=0.001,
        )
    )
    await wait_until(
        lambda: store.read("codex", "session-1") is not None,
        description="external session pin",
    )

    await asyncio.to_thread(
        service.arm,
        harness="codex",
        session="session-1",
        implies_return=True,
        turn_id="external-turn",
    )
    await wait_until(
        lambda: bool(
            (state := store.read("codex", "session-1")) is not None
            and state.stretch == 2
            and state.done.get("compact") == "ok"
        ),
        description="externally written arm completion",
    )
    alive[0] = False
    await task

    assert sensor.compact_calls == 1
    assert [notice.body for notice in sender.notices] == [
        "waiting on you",
        "cache cold in 15m",
        "compacted (100k → summary)",
    ]


@pytest.mark.asyncio
async def test_sidecar_continues_after_one_event_handler_failure(tmp_path: Path) -> None:
    alive = [True]
    clock = Clock()
    sender = Sender()
    service, store = policy(tmp_path, clock, sender)
    faulty = FailingOnceService(service)
    sensor = Sensor(
        (
            IdleEvent("turn_end", "session-1", "turn-1"),
            IdleEvent("turn_end", "session-1", "turn-2"),
        ),
        alive,
    )

    task = asyncio.create_task(
        run(
            sensor,
            context(tmp_path, alive),
            service=cast("IdleService", faulty),
            clock=cast("SidecarClock", clock),
            poll_seconds=0.001,
        )
    )
    await wait_until(
        lambda: task.done() or store.read("codex", "session-1") is not None,
        description="second event arm",
    )
    alive[0] = False
    await task

    state = store.read("codex", "session-1")
    assert state is not None
    assert state.last_turn_id is None
    assert "transient store failure" in (tmp_path / "p1" / "debug.jsonl").read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_sidecar_drops_events_for_another_session_and_records_once(tmp_path: Path) -> None:
    alive = [True]
    clock = Clock()
    sender = Sender()
    service, store = policy(tmp_path, clock, sender)
    sensor = Sensor(
        (
            IdleEvent("turn_end", "other-session", "other-turn-1"),
            IdleEvent("turn_end", "other-session", "other-turn-2"),
            IdleEvent("turn_end", "session-1", "turn-1"),
        ),
        alive,
    )

    task = asyncio.create_task(
        run(
            sensor,
            context(tmp_path, alive),
            service=service,
            clock=cast("SidecarClock", clock),
            poll_seconds=0.001,
        )
    )
    await wait_until(
        lambda: store.read("codex", "session-1") is not None,
        description="matching session arm",
    )
    alive[0] = False
    await task

    assert store.read("codex", "other-session") is None
    debug = (tmp_path / "p1" / "debug.jsonl").read_text(encoding="utf-8")
    assert debug.count("idle.session_mismatch") == 1


@pytest.mark.asyncio
async def test_sidecar_contains_base_exception_from_sensor(tmp_path: Path) -> None:
    alive = [True]
    clock = Clock()
    sender = Sender()
    service, _ = policy(tmp_path, clock, sender)

    task = asyncio.create_task(
        run(
            RaisingSensor((), alive),
            context(tmp_path, alive),
            service=service,
            clock=cast("SidecarClock", clock),
            poll_seconds=0.001,
        )
    )

    debug_path = tmp_path / "p1" / "debug.jsonl"
    await wait_until(debug_path.exists, description="sensor error record")
    alive[0] = False
    await task
    assert "SensorFailure" in debug_path.read_text(encoding="utf-8")
