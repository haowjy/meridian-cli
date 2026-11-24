"""Keepwarm through the real sidecar and a real ``IdleService``, with a fake clock and sensor."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Literal

from meridian.lib.config.models import MeridianConfig
from meridian.lib.harness.idle_types import IdleEvent
from meridian.lib.idle import keepwarm, sidecar
from tests.unit.idle.test_service import Clock, service

SESSION = "c1"


class JumpClock(Clock):
    """Sleeping to a deadline moves time there at once."""

    async def sleep_until_ms(self, deadline_ms: int) -> None:
        self.value = max(self.value, deadline_ms)
        await asyncio.sleep(0)


class Sensor:
    external_events = True

    def __init__(self, *, delivers: bool = True) -> None:
        self.events_in: asyncio.Queue[IdleEvent] = asyncio.Queue()
        self.sent: list[tuple[str, bool]] = []
        self.delivers = delivers
        self.running = False
        self.busy_after_send = False

    def on_raw_event(self, event: object) -> None:
        _ = event

    async def busy(self) -> bool:
        return self.running

    async def send(self, kind: Literal["reminder", "ping"], *, stop_offered: bool) -> bool:
        self.sent.append((kind, stop_offered))
        self.running = self.busy_after_send
        return self.delivers

    async def events(self) -> AsyncIterator[IdleEvent]:
        while True:
            yield await self.events_in.get()


def _turn(*, typed: str | None = None, replies: tuple[str, ...] = ()) -> IdleEvent:
    return IdleEvent(
        kind="turn_end",
        harness_session_id=SESSION,
        turn_id=typed,
        implies_return=typed is not None,
        prompt_replies=replies,
    )


def _run(
    sensor: Sensor,
    script: list[IdleEvent],
    *,
    keepwarm_on: bool = True,
    work_item: bool = False,
    pings: int = 3,
) -> None:
    idle_config = {
        "ttl_seconds": 600,
        "warn_minutes": 3,
        "keepwarm": keepwarm_on,
        "keepwarm_pings": pings,
    }
    config = MeridianConfig.model_validate({"harness": {"agy": {"idle": idle_config}}})
    svc, clock, _, _ = service(config=config)
    svc._has_work_item = lambda: work_item  # type: ignore[method-assign]
    jump = JumpClock()
    jump.value = clock.value = 1_000_000
    svc._now_ms = lambda: jump.value  # type: ignore[method-assign]
    alive = {"yes": True}
    ctx = SimpleNamespace(
        harness_id="agy",
        harness_session_id=SESSION,
        tui_alive=lambda: alive["yes"],
        env={},
        launch=None,
    )

    async def drive() -> None:
        task = asyncio.create_task(
            sidecar.run(
                sensor,  # type: ignore[arg-type]
                ctx,  # type: ignore[arg-type]
                service=svc,
                clock=jump,
                poll_seconds=100,
                error_reporter=SimpleNamespace(  # type: ignore[arg-type]
                    record=lambda **kw: (_ for _ in ()).throw(AssertionError(kw)),
                    record_session_mismatch=lambda **_: None,
                    close=lambda: None,
                ),
            )
        )
        for event in script:
            await sensor.events_in.put(event)
            quiet = 0
            while quiet < 4:  # the sidecar works in threads: wait until nothing more is sent
                before = len(sensor.sent)
                await asyncio.sleep(0.05)
                quiet = quiet + 1 if len(sensor.sent) == before and sensor.events_in.empty() else 0
        alive["yes"] = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(drive())


def test_pings_follow_the_warning_one_per_turn_end_up_to_the_limit() -> None:
    sensor = Sensor()
    _run(sensor, [_turn(typed="1"), _turn(), _turn(), _turn(), _turn()])

    assert sensor.sent == [("ping", True)] * 3


def test_a_ping_the_sensor_could_not_deliver_spends_no_slot() -> None:
    sensor = Sensor(delivers=False)
    _run(sensor, [_turn(typed="1"), _turn(), _turn(), _turn(), _turn()])

    assert len(sensor.sent) == 5  # the warning's and every later turn end's, none counted


def test_a_reply_ending_in_stop_waiting_ends_the_stretchs_pings() -> None:
    sensor = Sensor()
    _run(
        sensor,
        [
            _turn(typed="1"),
            _turn(replies=("Nothing to do.\n`stop-waiting`", "Watcher restarted.")),
            _turn(),
            _turn(),
        ],
    )

    assert sensor.sent == [("ping", True)]


def test_a_turn_running_at_the_timer_skips_the_ping_without_a_slot() -> None:
    sensor = Sensor()
    sensor.busy_after_send = True  # the ping's turn is still running when the next timer fires

    _run(sensor, [_turn(typed="1"), _turn(), _turn()])

    assert sensor.sent == [("ping", True)]  # the later timers found a turn running

    again = Sensor()
    _run(again, [_turn(typed="1"), _turn(), _turn()])
    assert len(again.sent) == 3  # with no running turn each end sends the next ping


def test_reminder_takes_the_first_slot_and_keepwarm_off_sends_only_the_reminder() -> None:
    on = Sensor()
    _run(on, [_turn(typed="1"), _turn(), _turn(), _turn()], work_item=True)
    assert on.sent == [("reminder", True), ("ping", True), ("ping", True)]

    off = Sensor()
    _run(off, [_turn(typed="1"), _turn(), _turn()], keepwarm_on=False, work_item=True)
    assert off.sent == [("reminder", False)]


def test_stop_reply_parsing_matches_the_claude_mod() -> None:
    for reply in ("stop-waiting", "  stop-waiting\n", '"stop-waiting"', "ok\n\n`stop-waiting`"):
        assert keepwarm.reply_stops(reply)
    for reply in (
        "stop-waiting.",
        "I will stop-waiting",
        '""stop-waiting""',
        "",
        "stop-waiting\nok",
    ):
        assert not keepwarm.reply_stops(reply)


def test_a_send_cancelled_by_a_turn_end_stays_spent() -> None:
    class Slow(Sensor):
        async def send(self, kind: Literal["reminder", "ping"], *, stop_offered: bool) -> bool:
            self.sent.append((kind, stop_offered))
            if len(self.sent) == 2:  # a turn ends while this ping's inbox write is in flight
                await self.events_in.put(_turn())
                await asyncio.sleep(0.2)
            return True

    sensor = Slow()
    _run(sensor, [_turn(typed="1"), _turn(), _turn(), _turn()], pings=2)

    assert len(sensor.sent) == 2


def test_a_reminder_that_could_not_be_sent_is_the_next_prompt_before_any_ping() -> None:
    class RefusesFirst(Sensor):
        async def send(self, kind: Literal["reminder", "ping"], *, stop_offered: bool) -> bool:
            self.sent.append((kind, stop_offered))
            return len(self.sent) > 1

    sensor = RefusesFirst()
    _run(sensor, [_turn(typed="1")] + [_turn()] * 4, work_item=True)

    assert [kind for kind, _ in sensor.sent] == ["reminder", "reminder", "ping", "ping"]
