"""Idle notices for a primary row a process holds, as ``agy``'s MCP server holds one.

The held row has no Meridian-run harness process, so the hosting process runs the idle
sidecar on its own loop, for the row's lifetime, with a sensor its harness provides.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from meridian.lib.harness.idle_types import IdleSensor, SidecarContext
from meridian.lib.idle import sidecar
from meridian.lib.idle.service import IdleService, close_on_launch


async def run_hold_idle(
    make_sensor: Callable[[str | None], IdleSensor],
    ctx: SidecarContext,
    *,
    readopted: bool = False,
) -> None:
    """Close the session's previous stretch, then drive idle policy until cancelled.

    Adopting a session in a new process is reopening it, which ends the stretch the
    previous process left open; a hold this process already had (``readopted``) is not.
    The sensor gets the turn the stored stretch last saw, read before the sidecar's first
    arm replaces it.
    """

    harness = str(ctx.harness_id)
    service = await asyncio.to_thread(IdleService, env=ctx.env, interactive=True)
    state = await asyncio.to_thread(service.store.read, harness, ctx.harness_session_id)
    if not readopted:
        await asyncio.to_thread(
            close_on_launch, harness=harness, session=ctx.harness_session_id, env=ctx.env
        )
    sensor = make_sensor(state.last_turn_id if state is not None else None)
    await sidecar.run(sensor, ctx, service=service)


__all__ = ["run_hold_idle"]
