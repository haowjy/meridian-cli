"""Process cleanup policy and action layer.

Owns all process termination decisions. Resolves scope snapshots to terminate
vs preserve, delegates to platform-specific backends, logs results.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from meridian.lib.core.types import SpawnId
from meridian.lib.platform.process_scope.base import CleanupResult, ProcessScopeSnapshot
from meridian.lib.platform.process_scope.fallback import terminate_tree_sync
from meridian.lib.state.process_scope_projection import (
    is_scope_released,
    mark_scope_released,
    read_scopes_from_disk,
)
from meridian.lib.state.spawn.model import SpawnRecord

logger = structlog.get_logger(__name__)


def terminate_spawn_scopes(
    runtime_root: Path,
    spawn_record: SpawnRecord,
    *,
    reason: str,
    grace_seconds: float = 5.0,
) -> list[CleanupResult]:
    """Terminate all spawn_owned scopes for a spawn record.

    Reads scope metadata from durable storage. For each spawn_owned scope,
    calls the appropriate platform terminator. Logs results.

    For legacy spawns with no scope metadata, falls back to worker_pid
    termination and logs degraded_fallback=True.
    """
    scopes = read_scopes_from_disk(runtime_root, SpawnId(spawn_record.id))
    results: list[CleanupResult] = []

    if not scopes:
        # Legacy fallback: no scope metadata, use worker_pid
        if spawn_record.worker_pid is not None and spawn_record.worker_pid > 0:
            result = _terminate_legacy_worker_pid(
                spawn_record,
                reason=reason,
                grace_seconds=grace_seconds,
            )
            results.append(result)
        return results

    spawn_id = SpawnId(spawn_record.id)
    for scope in scopes:
        if is_scope_released(runtime_root, spawn_id, scope.scope_id):
            result = CleanupResult(
                scope_id=scope.scope_id,
                root_pid=scope.root_pid,
                descendant_count=None,
                reason=reason,
                grace_seconds=0.0,
                kill_escalated=False,
                degraded_fallback=False,
                skip_reason="already_released",
            )
            logger.debug(
                "Skipped already-released process scope.",
                spawn_id=spawn_record.id,
                scope_id=scope.scope_id,
                root_pid=scope.root_pid,
            )
            results.append(result)
            continue

        if should_skip_cleanup(scope, spawn_record):
            result = CleanupResult(
                scope_id=scope.scope_id,
                root_pid=scope.root_pid,
                descendant_count=None,
                reason=reason,
                grace_seconds=0.0,
                kill_escalated=False,
                degraded_fallback=False,
                skip_reason="session_owned",
            )
            logger.info(
                "Skipped process scope cleanup.",
                spawn_id=spawn_record.id,
                scope_id=scope.scope_id,
                root_pid=scope.root_pid,
                skip_reason="session_owned",
            )
            results.append(result)
            continue

        result = terminate_tree_sync(
            pid=scope.root_pid,
            created_at_epoch=scope.root_created_at_epoch,
            grace_secs=grace_seconds,
            reason=reason,
            scope_id=scope.scope_id,
        )
        mark_scope_released(runtime_root, spawn_id, scope.scope_id)
        logger.info(
            "Terminated process scope.",
            spawn_id=spawn_record.id,
            scope_id=scope.scope_id,
            root_pid=scope.root_pid,
            descendant_count=result.descendant_count,
            reason=reason,
            grace_seconds=grace_seconds,
            kill_escalated=result.kill_escalated,
            degraded_fallback=result.degraded_fallback,
        )
        results.append(result)

    return results


def should_skip_cleanup(
    scope: ProcessScopeSnapshot,
    spawn_record: SpawnRecord,
) -> bool:
    """Return True for session_owned scopes.

    Phase 3 will make this richer with session lease lookup.
    """
    _ = spawn_record
    return scope.owner_policy == "session_owned"


def _terminate_legacy_worker_pid(
    spawn_record: SpawnRecord,
    *,
    reason: str,
    grace_seconds: float,
) -> CleanupResult:
    """Fallback for legacy spawns without scope metadata."""
    worker_pid = spawn_record.worker_pid
    if worker_pid is None or worker_pid <= 0:
        return CleanupResult(
            scope_id="legacy_worker",
            root_pid=0,
            descendant_count=None,
            reason=reason,
            grace_seconds=0.0,
            kill_escalated=False,
            degraded_fallback=True,
            skip_reason="no_worker_pid",
        )

    result = terminate_tree_sync(
        pid=worker_pid,
        created_at_epoch=0.0,  # legacy: no birth time available
        grace_secs=grace_seconds,
        reason=reason,
        scope_id="legacy_worker",
    )
    logger.warning(
        "Terminated legacy worker via degraded fallback.",
        spawn_id=spawn_record.id,
        worker_pid=worker_pid,
        degraded_fallback=True,
        reason=reason,
    )
    return result


def cancel_managed_primary(
    runtime_root: Path,
    spawn_record: SpawnRecord,
    *,
    grace_seconds: float = 10.0,
) -> list[CleanupResult]:
    """Cancel a managed primary — terminate launcher first, then backend/TUI.

    Sequenced teardown: launcher gets the first signal so any harness-driven
    session shutdown can propagate. After a brief pause, backend and TUI are
    terminated if they are still running.

    Idempotent — already-released scopes are skipped. Safe to call even when
    primary_metadata is not available (falls back to scope records only).
    """
    import time

    scopes = read_scopes_from_disk(runtime_root, SpawnId(spawn_record.id))
    results: list[CleanupResult] = []
    spawn_id = SpawnId(spawn_record.id)

    # 1. Terminate launcher scope first (spawn_owned).
    for scope in scopes:
        if scope.scope_id == "launcher":
            if is_scope_released(runtime_root, spawn_id, scope.scope_id):
                continue
            result = terminate_tree_sync(
                pid=scope.root_pid,
                created_at_epoch=scope.root_created_at_epoch,
                grace_secs=grace_seconds,
                reason="cancel",
                scope_id=scope.scope_id,
            )
            mark_scope_released(runtime_root, spawn_id, scope.scope_id)
            logger.info(
                "Terminated managed primary launcher scope.",
                spawn_id=spawn_record.id,
                scope_id=scope.scope_id,
                root_pid=scope.root_pid,
                reason="cancel",
            )
            results.append(result)

    # 2. Brief pause to let launcher-driven shutdown propagate.
    if results:
        time.sleep(1.0)

    # 3. Terminate backend and TUI if still unreleased.
    for scope in scopes:
        if scope.scope_id not in {"backend", "tui"}:
            continue
        if is_scope_released(runtime_root, spawn_id, scope.scope_id):
            result = CleanupResult(
                scope_id=scope.scope_id,
                root_pid=scope.root_pid,
                descendant_count=None,
                reason="cancel",
                grace_seconds=0.0,
                kill_escalated=False,
                degraded_fallback=False,
                skip_reason="already_released",
            )
            results.append(result)
            continue
        result = terminate_tree_sync(
            pid=scope.root_pid,
            created_at_epoch=scope.root_created_at_epoch,
            grace_secs=grace_seconds,
            reason="cancel",
            scope_id=scope.scope_id,
        )
        mark_scope_released(runtime_root, spawn_id, scope.scope_id)
        logger.info(
            "Terminated managed primary runtime scope.",
            spawn_id=spawn_record.id,
            scope_id=scope.scope_id,
            root_pid=scope.root_pid,
            reason="cancel",
        )
        results.append(result)

    return results


def reclaim_stale_session_scopes(
    runtime_root: Path,
    session_id: str,
    spawns: list[SpawnRecord],
    *,
    grace_seconds: float = 5.0,
) -> list[CleanupResult]:
    """Reclaim all session_owned scopes belonging to a stale session.

    Called when a session lease expires or a session is cleaned up. Walks all
    provided spawn records, finds unreleased session_owned scopes keyed to
    ``session_id``, terminates them, and marks them released.

    Idempotent — already-released scopes are skipped silently.
    """
    results: list[CleanupResult] = []
    for record in spawns:
        spawn_id = SpawnId(record.id)
        scopes = read_scopes_from_disk(runtime_root, spawn_id)
        for scope in scopes:
            if scope.owner_policy != "session_owned" or scope.owner_id != session_id:
                continue
            if is_scope_released(runtime_root, spawn_id, scope.scope_id):
                continue
            result = terminate_tree_sync(
                pid=scope.root_pid,
                created_at_epoch=scope.root_created_at_epoch,
                grace_secs=grace_seconds,
                reason="stale_session_cleanup",
                scope_id=scope.scope_id,
            )
            mark_scope_released(runtime_root, spawn_id, scope.scope_id)
            logger.info(
                "Reclaimed stale-session scope.",
                spawn_id=record.id,
                session_id=session_id,
                scope_id=scope.scope_id,
                root_pid=scope.root_pid,
                reason="stale_session_cleanup",
            )
            results.append(result)
    return results


__all__ = [
    "cancel_managed_primary",
    "reclaim_stale_session_scopes",
    "should_skip_cleanup",
    "terminate_spawn_scopes",
]
