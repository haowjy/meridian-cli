"""A message delivered to a session that starts a turn ends the session's open idle stretch.

The route reports how far the message got (``DeliveryKind``); this decides what that means
for idle state, as a turn starting does.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from meridian.lib.core.delivery import DeliveryKind
from meridian.lib.state import spawn_store
from meridian.lib.state.idle_store import IdleState, IdleStore
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.primary_meta import read_primary_surface_metadata
from meridian.lib.state.spawn.model import SpawnRecord

logger = logging.getLogger(__name__)


def row_session(runtime_root: Path, row: SpawnRecord) -> tuple[str | None, str | None]:
    """The harness session of a row and its reported activity; a primary's come from its
    metadata, as ``spawn list`` reads them."""

    session_id = row.harness_session_id
    activity: str | None = None
    if (row.kind or "").strip() == "primary":
        metadata = read_primary_surface_metadata(LaunchPaths.for_row(runtime_root, row))
        activity = metadata.activity
        session_id = metadata.harness_session_id or session_id
    return session_id, activity


async def end_stretch_on_delivery(
    runtime_root: Path, spawn_id: str, delivery: DeliveryKind
) -> None:
    """Close the spawn's open idle stretch when ``delivery`` starts a turn.

    ``queued`` starts none: the session reads the message at its next model call, which
    may be never, so the stretch's push, warn and keepwarm stay owed. A started turn warms
    the cache, so they no longer apply; the next turn that ends opens a new stretch.
    ``push_owed_since_ms`` is left alone: the human was not the one who returned.

    The message is already delivered, so a failure here is logged and never reaches the
    sender.
    """

    if delivery is DeliveryKind.QUEUED:
        return
    try:
        await asyncio.to_thread(_close_stretch, runtime_root, spawn_id)
    except Exception:
        logger.warning("Could not end the idle stretch of %s", spawn_id, exc_info=True)


def _close_stretch(runtime_root: Path, spawn_id: str) -> None:
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    if row is None or not row.harness:
        return
    session_id, _ = row_session(runtime_root, row)
    if not session_id:
        return

    def close(current: IdleState | None) -> tuple[IdleState | None, None]:
        if current is None or not current.stretch_open:
            return current, None
        return current.model_copy(update={"stretch_open": False}), None

    IdleStore().mutate(row.harness, session_id, close)
