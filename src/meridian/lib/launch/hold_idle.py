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

StoredTurnId = Callable[[], str | None]


async def run_hold_idle(
    make_sensor: Callable[[StoredTurnId], IdleSensor],
    ctx: SidecarContext,
) -> None:
    """Close the session's previous stretch, then drive idle policy until cancelled.

    Adopting a session is reopening it, which ends the stretch its previous process left
    open. The sensor reads the turn the stored stretch last saw through ``StoredTurnId``.
    """

    harness = str(ctx.harness_id)
    await asyncio.to_thread(
        close_on_launch, harness=harness, session=ctx.harness_session_id, env=ctx.env
    )
    service = await asyncio.to_thread(IdleService, env=ctx.env, interactive=True)

    def stored_turn_id() -> str | None:
        state = service.store.read(harness, ctx.harness_session_id)
        return state.last_turn_id if state is not None else None

    await sidecar.run(make_sensor(stored_turn_id), ctx, service=service)


__all__ = ["run_hold_idle"]
