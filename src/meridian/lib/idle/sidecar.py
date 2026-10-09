"""Launcher-hosted idle sensor task."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

from meridian.lib.harness.idle_types import IdleEvent, IdleSensor, IdleSensorContext
from meridian.lib.idle.service import ArmResult, IdleService
from meridian.lib.observability import DebugTracer
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

    def __init__(self, ctx: IdleSensorContext) -> None:
        self._tracer = DebugTracer(
            spawn_id=str(ctx.spawn_id or ""),
            debug_path=ctx.spawn_dir / "debug.jsonl",
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
    ctx: IdleSensorContext
    service: IdleService
    clock: SidecarClock
    harness: str
    error_reporter: SensorErrorReporter
    schedule_task: asyncio.Task[None] | None = None
    schedule_key: tuple[int, int] | None = None

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
                compact_at=None if "compact" in state.done else state.schedule.compact_at,
            )
        )

    async def _drive(self, arm: ArmResult) -> None:
        assert arm.stretch is not None
        assert arm.anchor is not None
        candidates: tuple[tuple[int | None, Stage], ...] = (
            (arm.push_at, "push"),
            (arm.warn_at, "warn"),
            (arm.compact_at, "compact"),
        )
        stages: list[tuple[int, Stage]] = [
            (deadline, stage) for deadline, stage in candidates if deadline is not None
        ]
        stages.sort()
        for deadline, stage in stages:
            await self.clock.sleep_until_ms(deadline)
            if not self.ctx.tui_alive():
                return
            if stage != "compact":
                self.service.fire(
                    stage,
                    harness=self.harness,
                    session=self.ctx.harness_session_id,
                    stretch=arm.stretch,
                    anchor=arm.anchor,
                )
                continue

            facts = await self.sensor.facts()
            decision = self.service.fire(
                "compact",
                harness=self.harness,
                session=self.ctx.harness_session_id,
                stretch=arm.stretch,
                anchor=arm.anchor,
                facts=facts,
            )
            if decision.decision != "act":
                continue
            try:
                result = await self.sensor.compact()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                self.error_reporter.record(phase="compact", error=exc)
                self.service.done(
                    "compact",
                    harness=self.harness,
                    session=self.ctx.harness_session_id,
                    stretch=arm.stretch,
                    result="failed",
                    detail=type(exc).__name__,
                )
                continue
            self.service.done(
                "compact",
                harness=self.harness,
                session=self.ctx.harness_session_id,
                stretch=arm.stretch,
                result=result.result,
                detail=result.reason,
            )

    async def _drive_safely(self, arm: ArmResult) -> None:
        try:
            await self._drive(arm)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self.error_reporter.record(phase="timer", error=exc)

    async def handle(self, event: IdleEvent) -> None:
        result = self.service.event(event, harness=self.harness)
        if event.kind == "user_prompt":
            await self.cancel_schedule()
            return
        if isinstance(result, ArmResult):
            await self.apply_arm(result)


async def _consume_events(sensor: IdleSensor, coordinator: _Coordinator) -> None:
    async for event in sensor.events():
        if not coordinator.ctx.tui_alive():
            return
        await coordinator.handle(event)


async def _poll_store(coordinator: _Coordinator, poll_seconds: float) -> None:
    while coordinator.ctx.tui_alive():
        await asyncio.sleep(poll_seconds)
        states = coordinator.service.status(
            harness=coordinator.harness,
            session=coordinator.ctx.harness_session_id,
        )
        await coordinator.apply_state(states[0] if states else None)


async def run(
    sensor: IdleSensor,
    ctx: IdleSensorContext,
    *,
    service: IdleService | None = None,
    clock: SidecarClock | None = None,
    poll_seconds: float = 5.0,
    error_reporter: SensorErrorReporter | None = None,
) -> None:
    """Drive idle policy until launcher teardown; contain every sensor failure."""

    resolved_clock = clock if clock is not None else _RealClock()
    resolved_service = service or IdleService(
        env=ctx.env,
        now_ms=resolved_clock.now_ms,
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
    tasks = {
        asyncio.create_task(_consume_events(sensor, coordinator)),
        asyncio.create_task(_poll_store(coordinator, poll_seconds)),
    }
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
        finally:
            if error_reporter is None:
                resolved_error_reporter.close()


__all__ = ["SensorErrorReporter", "SidecarClock", "run"]
