from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
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
from meridian.lib.idle.service import IdleService, NoticeSpec, NotifyReport
from meridian.lib.idle.sidecar import SensorErrorReporter, SidecarClock, run
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
        return CompactResult("ok", "100k → summary")


class RaisingSensor(Sensor):
    async def events(self) -> AsyncIterator[IdleEvent]:
        if False:
            yield IdleEvent("idle", "unused", None, 0)
        raise SensorFailure("broken stream")


class RaisingFactsSensor(Sensor):
    async def facts(self) -> IdleFacts:
        self._alive[0] = False
        raise SensorFailure("broken facts")


class BlockingCompactionSensor(Sensor):
    def __init__(self, alive: list[bool]) -> None:
        super().__init__((), alive)
        self.compact_started = asyncio.Event()
        self.release_compact = asyncio.Event()
        self.compact_cancelled = False

    async def events(self) -> AsyncIterator[IdleEvent]:
        yield IdleEvent("turn_end", "session-1", "turn-1", 0)
        await self.compact_started.wait()
        yield IdleEvent("user_prompt", "session-1", None, 1)
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


class PersistentSensor(Sensor):
    async def compact(self) -> CompactResult:
        self.compact_calls += 1
        return CompactResult("ok", "100k → summary")


class ExternalSensor(Sensor):
    external_events = True


class RecordingService:
    def __init__(self, delegate: IdleService) -> None:
        self.delegate = delegate
        self.thread_ids: dict[str, list[int]] = {}

    def _record(self, method: str) -> None:
        self.thread_ids.setdefault(method, []).append(threading.get_ident())

    def event(self, *args: Any, **kwargs: Any) -> Any:
        self._record("event")
        return self.delegate.event(*args, **kwargs)

    def arm(self, *args: Any, **kwargs: Any) -> Any:
        self._record("arm")
        return self.delegate.arm(*args, **kwargs)

    def status(self, *args: Any, **kwargs: Any) -> Any:
        self._record("status")
        return self.delegate.status(*args, **kwargs)

    def fire(self, *args: Any, **kwargs: Any) -> Any:
        self._record("fire")
        return self.delegate.fire(*args, **kwargs)

    def done(self, *args: Any, **kwargs: Any) -> Any:
        self._record("done")
        return self.delegate.done(*args, **kwargs)


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
    sensor = Sensor((IdleEvent("turn_end", "session-1", "turn-1", 0),), alive)

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
    await wait_until(
        lambda: bool(
            (state := store.read("codex", "session-1")) is not None
            and not state.stretch_open
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
            (state := store.read("codex", "session-1")) is not None
            and not state.stretch_open
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
async def test_sidecar_offloads_every_synchronous_service_call(tmp_path: Path) -> None:
    alive = [True]
    clock = Clock()
    sender = Sender()
    service, _ = policy(tmp_path, clock, sender)
    recording = RecordingService(service)
    sensor = ExternalSensor(
        (IdleEvent("turn_end", "session-1", "turn-1", 0),),
        alive,
    )
    loop_thread = threading.get_ident()

    task = asyncio.create_task(
        run(
            sensor,
            context(tmp_path, alive),
            service=cast("IdleService", recording),
            clock=cast("SidecarClock", clock),
            poll_seconds=0.001,
        )
    )
    await wait_until(
        lambda: {"arm", "event", "status", "fire", "done"} <= recording.thread_ids.keys(),
        description="all service calls",
    )
    alive[0] = False
    await task

    assert all(
        thread_id != loop_thread
        for thread_ids in recording.thread_ids.values()
        for thread_id in thread_ids
    )


@pytest.mark.asyncio
async def test_external_event_sensor_pins_session_and_polls_store(tmp_path: Path) -> None:
    alive = [True]
    clock = Clock()
    sender = Sender()
    service, store = policy(tmp_path, clock, sender)
    recording = RecordingService(service)
    sensor = ExternalSensor((), alive)
    loop_thread = threading.get_ident()

    task = asyncio.create_task(
        run(
            sensor,
            context(tmp_path, alive),
            service=cast("IdleService", recording),
            clock=cast("SidecarClock", clock),
            poll_seconds=0.001,
        )
    )
    await wait_until(
        lambda: bool(
            (state := store.read("codex", "session-1")) is not None
            and state.main_thread_id == "session-1"
        ),
        description="external session pin",
    )
    await wait_until(
        lambda: "status" in recording.thread_ids,
        description="external store poll",
    )
    alive[0] = False
    await task

    state = store.read("codex", "session-1")
    assert state is not None
    assert state.spawn_id == "p1"
    assert sender.notices == []
    assert sensor.compact_calls == 0
    assert recording.thread_ids["arm"][0] != loop_thread
    assert recording.thread_ids["status"][0] != loop_thread


@pytest.mark.asyncio
async def test_sidecar_continues_after_one_event_handler_failure(tmp_path: Path) -> None:
    alive = [True]
    clock = Clock()
    sender = Sender()
    service, store = policy(tmp_path, clock, sender)
    faulty = FailingOnceService(service)
    sensor = Sensor(
        (
            IdleEvent("turn_end", "session-1", "turn-1", 0),
            IdleEvent("turn_end", "session-1", "turn-2", 1),
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
    assert "transient store failure" in (
        tmp_path / "p1" / "debug.jsonl"
    ).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_sidecar_drops_events_for_another_session_and_records_once(tmp_path: Path) -> None:
    alive = [True]
    clock = Clock()
    sender = Sender()
    service, store = policy(tmp_path, clock, sender)
    sensor = Sensor(
        (
            IdleEvent("turn_end", "other-session", "other-turn-1", 0),
            IdleEvent("turn_end", "other-session", "other-turn-2", 1),
            IdleEvent("turn_end", "session-1", "turn-1", 2),
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


@pytest.mark.asyncio
async def test_sidecar_contains_base_exception_from_sensor_facts(tmp_path: Path) -> None:
    alive = [True]
    clock = Clock()
    sender = Sender()
    service, _ = policy(tmp_path, clock, sender)
    sensor = RaisingFactsSensor(
        (IdleEvent("turn_end", "session-1", "turn-1", 0),),
        alive,
    )

    await run(
        sensor,
        context(tmp_path, alive),
        service=service,
        clock=cast("SidecarClock", clock),
        poll_seconds=0.001,
    )

    assert sensor.compact_calls == 0
    assert "broken facts" in (tmp_path / "p1" / "debug.jsonl").read_text(encoding="utf-8")


def test_sensor_error_reporter_rate_limits_repeated_failures(tmp_path: Path) -> None:
    reporter = SensorErrorReporter(context(tmp_path, [True]))
    for _ in range(3):
        reporter.record(phase="raw_event", error=RuntimeError("repeated"))
    reporter.close()
    reporter.close()

    records = [
        json.loads(line)
        for line in (tmp_path / "p1" / "debug.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [record["event"] for record in records] == [
        "idle.sensor_error",
        "idle.sensor_error_repeats",
    ]
    assert records[1]["data"]["repeat_count"] == 2
