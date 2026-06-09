"""Process cleanup for Pi-tracked child work."""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from meridian.lib.core.types import SpawnId
from meridian.lib.streaming.pi_subspawn_tracker import PiSubspawnTracker

logger = logging.getLogger(__name__)


async def terminate_pi_tracked_subspawns(
    spawn_id: SpawnId,
    tracker: PiSubspawnTracker,
    *,
    reason: str,
    exclude_subspawn_ids: set[str] | None = None,
) -> None:
    pgids = tracker.active_tracked_pgid_candidates(exclude_ids=exclude_subspawn_ids)
    if not pgids:
        logger.warning(
            "Pi spawn %s ended with tracked children but no pid/pgid metadata for cleanup",
            spawn_id,
        )
        return

    for pgid in pgids:
        if os.name == "nt":
            await _terminate_process_tree_fallback(
                spawn_id=spawn_id,
                process_id=pgid,
                reason=reason,
            )
        else:
            await _terminate_posix_process_group(
                spawn_id=spawn_id,
                process_group_id=pgid,
                reason=reason,
            )


async def _terminate_process_tree_fallback(
    *,
    spawn_id: SpawnId,
    process_id: int,
    reason: str,
) -> None:
    if process_id <= 0:
        return

    try:
        from meridian.lib.platform.process_scope.fallback import terminate_tree_sync

        await asyncio.to_thread(
            terminate_tree_sync,
            pid=process_id,
            grace_secs=5.0,
            reason=reason,
            scope_id=f"pi-subspawn:{spawn_id}",
            degraded_fallback=True,
        )
    except Exception:
        logger.warning(
            "Failed fallback cleanup for Pi child process %d (spawn %s, reason=%s)",
            process_id,
            spawn_id,
            reason,
            exc_info=True,
        )


async def _terminate_posix_process_group(
    *,
    spawn_id: SpawnId,
    process_group_id: int,
    reason: str,
) -> None:
    if os.name == "nt" or process_group_id <= 0:
        return

    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    except (PermissionError, OSError):
        logger.warning(
            "Failed SIGTERM cleanup for Pi child process group %d (spawn %s, reason=%s)",
            process_group_id,
            spawn_id,
            reason,
            exc_info=True,
        )
        return

    await asyncio.sleep(0.25)

    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return
    except (PermissionError, OSError):
        logger.warning(
            "Failed liveness check for Pi child process group %d (spawn %s, reason=%s)",
            process_group_id,
            spawn_id,
            reason,
            exc_info=True,
        )
        return

    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return
    except (PermissionError, OSError):
        logger.warning(
            "Failed SIGKILL cleanup for Pi child process group %d (spawn %s, reason=%s)",
            process_group_id,
            spawn_id,
            reason,
            exc_info=True,
        )
