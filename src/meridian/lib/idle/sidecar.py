from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

from meridian.lib.harness.idle_types import (
    DetectIdleTtl,
    IdleEvent,
    IdleSensor,
    PromptSink,
    SidecarContext,
)
from meridian.lib.idle.keepwarm import EXPIRY_MARGIN_MS, Stretch, reply_stops
from meridian.lib.idle.service import ArmResult, IdleService
from meridian.lib.observability.debug_tracer import DebugTracer
from meridian.lib.state.idle_store import IdleState, Stage


class SidecarClock(Protocol):
    """Clock seam for deterministic absolute-deadline tests."""

    def now_ms(self) -> int: ...

    async def sleep_until_ms(self, deadline_ms: int) -> None: ...


class _RealClock:
    def now_ms(self) -> int:
        return int(time.time() * 1000)

    async def sleep_until_ms(self, deadline_ms: int) -> None:
        delay = max(0.0, (deadline_ms - self.now_ms()) / 1000)
        await asyncio.sleep(delay)


class SensorErrorReporter:
    """Task-scoped, repeat-limited sensor diagnostics."""

    def __init__(self, ctx: SidecarContext) -> None:
        self._tracer = DebugTracer(
            spawn_id=str(ctx.launch.spawn_id),
            debug_path=ctx.launch.debug,
            report_failures=False,
        )
        self._counts: dict[tuple[str, str, str], int] = {}
        self._closed = False

    def record(self, *, phase: str, error: BaseException) -> None:
        """Emit the first matching failure and count later repeats."""

        if self._closed:
            return
        key = (phase, type(error).__name__, str(error))
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count
        if count > 1:
            return
        with suppress(BaseException):
            self._tracer.emit(
                "idle",
                "idle.sensor_error",
                data={
                    "phase": phase,
                    "error_type": key[1],
                    "error": key[2],
                },
            )

    def record_session_mismatch(self, *, expected: str, received: str) -> None:

        if self._closed:
            return
        with suppress(BaseException):
            self._tracer.emit(
                "idle",
                "idle.session_mismatch",
                data={"expected": expected, "received": received},
            )

    def close(self) -> None:
        """Emit repeat summaries and close the tracer. Idempotent."""

        if self._closed:
            return
        self._closed = True
        for (phase, error_type, error), count in self._counts.items():
            if count < 2:
                continue
            with suppress(BaseException):
                self._tracer.emit(
                    "idle",
                    "idle.sensor_error_repeats",
                    data={
                        "phase": phase,
                        "error_type": error_type,
                        "error": error,
                        "repeat_count": count - 1,
                    },
                )
        with suppress(BaseException):
            self._tracer.close()


@dataclass
class _Coordinator:
    sensor: IdleSensor
    ctx: SidecarContext
    service: IdleService
    clock: SidecarClock
    harness: str
    error_reporter: SensorErrorReporter
    schedule_task: asyncio.Task[None] | None = None
    schedule_key: tuple[int, int] | None = None
    session_mismatch_recorded: bool = False
    keepwarm: Stretch | None = None
    ping_task: asyncio.Task[None] | None = None

    async def cancel_ping(self) -> None:
        task = self.ping_task
        self.ping_task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task

    async def cancel_schedule(self) -> None:
        task = self.schedule_task
        self.schedule_task = None
        self.schedule_key = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task

    async def apply_arm(self, result: ArmResult) -> None:
        if result.stretch is None or result.anchor is None:
            await self.cancel_schedule()
            return
        key = (result.stretch, result.anchor)
        if key == self.schedule_key:
            return
        await self.cancel_schedule()
        self.schedule_key = key
        self.schedule_task = asyncio.create_task(self._drive_safely(result))

    async def apply_state(self, state: IdleState | None) -> None:
        if state is None:
            await self.cancel_schedule()
            return
        await self.apply_arm(
            ArmResult(
                stretch=state.stretch,
                anchor=state.anchor,
                push_at=None if "push" in state.done else state.schedule.push_at,
                warn_at=None if "warn" in state.done else state.schedule.warn_at,
            )
        )

    async def _drive(self, arm: ArmResult) -> None:
        assert arm.stretch is not None
        assert arm.anchor is not None
        candidates: tuple[tuple[int | None, Stage], ...] = (
            (arm.push_at, "push"),
            (arm.warn_at, "warn"),
        )
        stages: list[tuple[int, Stage]] = [
            (deadline, stage) for deadline, stage in candidates if deadline is not None
        ]
        stages.sort()
        for deadline, stage in stages:
            await self.clock.sleep_until_ms(deadline)
            if not self.ctx.tui_alive():
                return
            if getattr(self.sensor, "external_events", False) and await self.sensor.busy():
                continue
            fired = await asyncio.to_thread(
                self.service.fire,
                stage,
                harness=self.harness,
                session=self.ctx.harness_session_id,
                stretch=arm.stretch,
                anchor=arm.anchor,
            )
            if stage == "warn" and fired.decision == "act":
                await self._warned(arm, remind=fired.remind)

    async def _warned(self, arm: ArmResult, *, remind: bool) -> None:
        """The warning acted: send the state reminder, else ping 1 when keepwarm is on. The
        reminder takes ping 1's slot with keepwarm on and none with it off."""

        if not isinstance(self.sensor, PromptSink):
            return
        kw = self.keepwarm if self.keepwarm and self.keepwarm.stretch == arm.stretch else None
        on = kw is not None and kw.limit > 0
        if kw is not None:
            kw.warned = True
        if not (remind or on):
            return
        # The slot is taken before the send and given back only when the sensor says nothing
        # was queued: a send cancelled mid-way (its inbox write still lands) stays spent, so
        # a stretch can lose a ping but never exceed its limit.
        if kw is not None and on:
            kw.sent += 1
        delivered = await self.sensor.send("reminder" if remind else "ping", stop_offered=on)
        if not delivered:
            if kw is not None and on:
                kw.sent -= 1
            if kw is not None and remind:
                kw.reminder_owed = True

    async def _track(self, event: IdleEvent, result: ArmResult) -> None:
        """Keep the stretch's keepwarm record from an arm: its start, its lead, its reply."""

        if result.stretch is None or not isinstance(self.sensor, PromptSink):
            return
        policy = (await asyncio.to_thread(self.service.config_for, self.harness)).policy
        kw = self.keepwarm
        if kw is None or kw.stretch != result.stretch:
            await self.cancel_ping()
            limit = policy.keepwarm_pings if policy.keepwarm else 0
            kw = self.keepwarm = Stretch(result.stretch, limit)
        now = self.clock.now_ms()
        if kw.lead_ms is None and result.warn_at is not None:
            kw.lead_ms = result.warn_at - now
        if any(reply_stops(reply) for reply in event.prompt_replies):
            kw.ended = True
        if kw.ended or event.implies_return:
            await self.cancel_ping()
        elif kw.due() and policy.warn_minutes * 60_000 > EXPIRY_MARGIN_MS:
            await self.cancel_ping()
            assert kw.lead_ms is not None
            fire_at = now + kw.lead_ms
            deadline = fire_at + policy.warn_minutes * 60_000 - EXPIRY_MARGIN_MS
            self.ping_task = asyncio.create_task(self._ping_safely(kw, fire_at, deadline))

    async def _ping_safely(self, kw: Stretch, fire_at: int, deadline: int) -> None:
        try:
            await self._ping(kw, fire_at, deadline)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self.error_reporter.record(phase="keepwarm", error=exc)

    async def _ping(self, kw: Stretch, fire_at: int, deadline: int) -> None:
        """One later prompt (an owed reminder, else a ping), one lead after a turn that ended in
        this stretch. A turn running then skips it without a slot: that turn's own end
        schedules the next."""

        assert isinstance(self.sensor, PromptSink)
        await self.clock.sleep_until_ms(fire_at)
        if not self.ctx.tui_alive() or kw.ended:
            return
        if self.clock.now_ms() >= deadline:
            kw.ended = True  # the cache is cold: further pings would only prefill again
            return
        if getattr(self.sensor, "external_events", False) and await self.sensor.busy():
            return
        # Something else may have closed the stretch (a new process, ``meridian idle return``):
        # only the store says so.
        states = await asyncio.to_thread(
            self.service.status, harness=self.harness, session=self.ctx.harness_session_id
        )
        if not states or not states[0].stretch_open or states[0].stretch != kw.stretch:
            kw.ended = True
            return
        kind = kw.next_kind()
        if kind is None:
            return
        on = kw.limit > 0
        # Taken before the send, as in ``_warned``; the reminder takes a slot only with keepwarm on.
        if on:
            kw.sent += 1
        kw.reminder_owed = kw.reminder_owed and kind != "reminder"
        if not await self.sensor.send(kind, stop_offered=on):
            if on:
                kw.sent -= 1
            kw.reminder_owed = kw.reminder_owed or kind == "reminder"

    async def _drive_safely(self, arm: ArmResult) -> None:
        try:
            await self._drive(arm)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self.error_reporter.record(phase="timer", error=exc)

    async def handle(self, event: IdleEvent) -> None:
        if event.harness_session_id != self.ctx.harness_session_id:
            if not self.session_mismatch_recorded:
                self.session_mismatch_recorded = True
                self.error_reporter.record_session_mismatch(
                    expected=self.ctx.harness_session_id,
                    received=event.harness_session_id,
                )
            return
        result = await asyncio.to_thread(
            self.service.event,
            event,
            harness=self.harness,
        )
        if event.kind == "user_prompt":
            await self.cancel_schedule()
            await self.cancel_ping()
            self.keepwarm = None
            return
        if isinstance(result, ArmResult):
            # The record first: the arm may start the stage timer that reads it.
            await self._track(event, result)
            await self.apply_arm(result)


async def _consume_events(sensor: IdleSensor, coordinator: _Coordinator) -> None:
    try:
        async for event in sensor.events():
            if not coordinator.ctx.tui_alive():
                return
            try:
                await coordinator.handle(event)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                coordinator.error_reporter.record(phase="event", error=exc)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        coordinator.error_reporter.record(phase="events", error=exc)


async def _poll_store(coordinator: _Coordinator, poll_seconds: float) -> None:
    while coordinator.ctx.tui_alive():
        await asyncio.sleep(poll_seconds)
        try:
            states = await asyncio.to_thread(
                coordinator.service.status,
                harness=coordinator.harness,
                session=coordinator.ctx.harness_session_id,
            )
            await coordinator.apply_state(states[0] if states else None)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            coordinator.error_reporter.record(phase="store_poll", error=exc)


async def run(
    sensor: IdleSensor,
    ctx: SidecarContext,
    *,
    service: IdleService | None = None,
    clock: SidecarClock | None = None,
    poll_seconds: float = 5.0,
    error_reporter: SensorErrorReporter | None = None,
    detect_ttl: DetectIdleTtl | None = None,
) -> None:
    """Drive idle policy until launcher teardown; contain every sensor failure."""

    resolved_clock = clock if clock is not None else _RealClock()
    resolved_service = service or IdleService(
        env=ctx.env,
        now_ms=resolved_clock.now_ms,
        detect_ttl=detect_ttl,
    )
    resolved_error_reporter = error_reporter or SensorErrorReporter(ctx)
    coordinator = _Coordinator(
        sensor=sensor,
        ctx=ctx,
        service=resolved_service,
        clock=resolved_clock,
        harness=str(ctx.harness_id),
        error_reporter=resolved_error_reporter,
    )
    external_events = bool(getattr(sensor, "external_events", False))
    tasks: set[asyncio.Task[None]] = set()
    if external_events:
        try:
            initial = await asyncio.to_thread(
                resolved_service.arm,
                harness=str(ctx.harness_id),
                session=ctx.harness_session_id,
            )
            # This first state is an identity pin, not an observed idle turn.
            # Remember its version so polling ignores it until an external
            # event advances the anchor or stretch.
            if initial.stretch is not None and initial.anchor is not None:
                coordinator.schedule_key = (initial.stretch, initial.anchor)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            resolved_error_reporter.record(phase="external_start", error=exc)
        tasks.add(asyncio.create_task(_poll_store(coordinator, poll_seconds)))
    tasks.add(asyncio.create_task(_consume_events(sensor, coordinator)))
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        resolved_error_reporter.record(phase="run", error=exc)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(BaseException):
                await task
        try:
            await coordinator.cancel_schedule()
            await coordinator.cancel_ping()
        finally:
            if error_reporter is None:
                resolved_error_reporter.close()


__all__ = ["SensorErrorReporter", "SidecarClock", "run"]
