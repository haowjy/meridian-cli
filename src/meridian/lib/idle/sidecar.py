"""Launcher-hosted idle sensor task."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from meridian.lib.harness.idle_types import IdleSensor, IdleSensorContext
from meridian.lib.observability import DebugTracer


def record_sensor_error(
    ctx: IdleSensorContext,
    *,
    phase: str,
    error: BaseException,
) -> None:
    """Best-effort sensor diagnostics that never touch the TUI's stderr."""

    tracer = DebugTracer(
        spawn_id=ctx.spawn_dir.name,
        debug_path=ctx.spawn_dir / "debug.jsonl",
        report_failures=False,
    )
    with suppress(BaseException):
        tracer.emit(
            "idle",
            "idle.sensor_error",
            data={
                "phase": phase,
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )
    with suppress(BaseException):
        tracer.close()


async def run(sensor: IdleSensor, ctx: IdleSensorContext) -> None:
    """Drain sensor events until launcher teardown or a sensor failure."""

    try:
        async for _event in sensor.events():
            pass
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        record_sensor_error(ctx, phase="run", error=exc)


__all__ = ["record_sensor_error", "run"]
