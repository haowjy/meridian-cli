"""Process cleanup policy and action layer.

Owns all process termination decisions. Resolves scope snapshots to terminate
vs preserve, delegates to platform-specific backends, logs results.
"""

from __future__ import annotations

from pathlib import Path

import psutil
import structlog

from meridian.lib.core.types import SpawnId
from meridian.lib.platform.process_scope import terminate_scope_sync, terminate_tree_sync
from meridian.lib.platform.process_scope.base import (
    CleanupResult,
    ProcessScopeSnapshot,
    birth_time_unverified,
)
from meridian.lib.state.process_scope_projection import (
    is_scope_released,
    mark_scope_released,
    read_scopes_from_disk,
)
from meridian.lib.state.spawn.model import SpawnRecord

logger = structlog.get_logger(__name__)


def terminate_recorded_spawn_scopes(
    runtime_root: Path,
    spawn_record: SpawnRecord,
    *,
    reason: str,
    grace_seconds: float = 5.0,
) -> list[CleanupResult]:
    """Terminate scope sidecars recorded for a spawn record."""

    scopes = read_scopes_from_disk(runtime_root, SpawnId(spawn_record.id))
    return _terminate_recorded_spawn_scopes(
        runtime_root,
        spawn_record,
        scopes,
        reason=reason,
        grace_seconds=grace_seconds,
    )


def terminate_legacy_worker_fallback(
    spawn_record: SpawnRecord,
    *,
    reason: str,
    grace_seconds: float = 5.0,
) -> CleanupResult | None:
    """Terminate a legacy spawn worker when no scope sidecars exist."""

    if spawn_record.worker_pid is None or spawn_record.worker_pid <= 0:
        return None
    return _terminate_legacy_worker_pid(
        spawn_record,
        reason=reason,
        grace_seconds=grace_seconds,
    )


def terminate_spawn_scopes(
    runtime_root: Path,
    spawn_record: SpawnRecord,
    *,
    reason: str,
    grace_seconds: float = 5.0,
) -> list[CleanupResult]:
    """Terminate recorded scopes, with legacy worker fallback when none exist."""

    scopes = read_scopes_from_disk(runtime_root, SpawnId(spawn_record.id))
    if scopes:
        return _terminate_recorded_spawn_scopes(
            runtime_root,
            spawn_record,
            scopes,
            reason=reason,
            grace_seconds=grace_seconds,
        )

    legacy_result = terminate_legacy_worker_fallback(
        spawn_record,
        reason=reason,
        grace_seconds=grace_seconds,
    )
    if legacy_result is None:
        return []
    return [legacy_result]


def _terminate_recorded_spawn_scopes(
    runtime_root: Path,
    spawn_record: SpawnRecord,
    scopes: list[ProcessScopeSnapshot],
    *,
    reason: str,
    grace_seconds: float,
) -> list[CleanupResult]:
    results: list[CleanupResult] = []
    spawn_id = SpawnId(spawn_record.id)

    for scope in scopes:
        if is_scope_released(runtime_root, spawn_id, scope.release_id):
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
                descendant_count=None,
                reason=reason,
                grace_seconds=0.0,
                kill_escalated=False,
                degraded_fallback=False,
                skip_reason="already_released",
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
                skip_reason="active_session_lease",
            )
            logger.debug(
                "Skipped process scope cleanup — session lease active.",
                spawn_id=spawn_record.id,
                scope_id=scope.scope_id,
                root_pid=scope.root_pid,
                descendant_count=None,
                reason=reason,
                grace_seconds=0.0,
                kill_escalated=False,
                degraded_fallback=False,
                skip_reason="active_session_lease",
            )
            results.append(result)
            continue

        result = terminate_scope_sync(scope, grace_seconds=grace_seconds, reason=reason)
        mark_scope_released(runtime_root, spawn_id, scope.release_id)
        logger.debug(
            "Terminated process scope.",
            spawn_id=spawn_record.id,
            scope_id=scope.scope_id,
            root_pid=scope.root_pid,
            descendant_count=result.descendant_count,
            reason=reason,
            grace_seconds=grace_seconds,
            kill_escalated=result.kill_escalated,
            degraded_fallback=result.degraded_fallback,
            skip_reason=result.skip_reason,
        )
        results.append(result)

    return results


def should_skip_cleanup(
    scope: ProcessScopeSnapshot,
    spawn_record: SpawnRecord,
) -> bool:
    """Return True when a session_owned scope should be preserved.

    A ``session_owned`` scope is preserved while the managed primary runtime
    is considered active.  However, if the root process died independently
    (e.g. crashed while the lease was still valid), there is nothing to
    preserve — the scope must be reclaimed rather than silently leaked
    (PROC-007).

    Validation order:
    1. Only ``session_owned`` scopes are candidates for skip.
    2. If the root PID is dead or its birth time has drifted (PID reuse),
       return False so the caller reclaims the already-gone scope.
    3. Otherwise return True (preserve — process is alive and verified).
    """
    if scope.owner_policy != "session_owned":
        return False

    try:
        actual = psutil.Process(scope.root_pid).create_time()
        if abs(actual - scope.root_created_at_epoch) > 1.0:
            # PID was recycled by another process after the scope's root died.
            logger.debug(
                "session_owned scope root PID reused — reclaiming.",
                spawn_id=spawn_record.id,
                scope_id=scope.scope_id,
                root_pid=scope.root_pid,
                skip_reason="session_owned_process_dead",
            )
            return False
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        # Root process is gone — reclaim despite the session_owned policy.
        logger.debug(
            "session_owned scope root process dead — reclaiming.",
            spawn_id=spawn_record.id,
            scope_id=scope.scope_id,
            root_pid=scope.root_pid,
            skip_reason="session_owned_process_dead",
        )
        return False

    return True


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
    logger.debug(
        "Cleaned up spawn via legacy worker fallback.",
        spawn_id=spawn_record.id,
        affected_spawn_id=spawn_record.id,
        cleanup_target="stale_spawn",
        scope_id=result.scope_id,
        root_pid=worker_pid,
        descendant_count=result.descendant_count,
        reason=reason,
        grace_seconds=grace_seconds,
        kill_escalated=result.kill_escalated,
        degraded_fallback=True,
        skip_reason=result.skip_reason,
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
            if is_scope_released(runtime_root, spawn_id, scope.release_id):
                continue
            result = terminate_scope_sync(scope, grace_seconds=grace_seconds, reason="cancel")
            mark_scope_released(runtime_root, spawn_id, scope.release_id)
            logger.debug(
                "Terminated managed primary launcher scope.",
                spawn_id=spawn_record.id,
                scope_id=scope.scope_id,
                root_pid=scope.root_pid,
                descendant_count=result.descendant_count,
                reason="cancel",
                grace_seconds=grace_seconds,
                kill_escalated=result.kill_escalated,
                degraded_fallback=result.degraded_fallback,
                skip_reason=result.skip_reason,
            )
            results.append(result)

    # 2. Brief pause to let launcher-driven shutdown propagate.
    if results:
        time.sleep(1.0)

    # 3. Terminate backend and TUI if still unreleased.
    for scope in scopes:
        if scope.scope_id not in {"backend", "tui"}:
            continue
        if is_scope_released(runtime_root, spawn_id, scope.release_id):
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
            logger.debug(
                "Skipped already-released managed primary runtime scope.",
                spawn_id=spawn_record.id,
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
        result = terminate_scope_sync(scope, grace_seconds=grace_seconds, reason="cancel")
        mark_scope_released(runtime_root, spawn_id, scope.release_id)
        logger.debug(
            "Terminated managed primary runtime scope.",
            spawn_id=spawn_record.id,
            scope_id=scope.scope_id,
            root_pid=scope.root_pid,
            descendant_count=result.descendant_count,
            reason="cancel",
            grace_seconds=grace_seconds,
            kill_escalated=result.kill_escalated,
            degraded_fallback=result.degraded_fallback,
            skip_reason=result.skip_reason,
        )
        results.append(result)

    return results


def reclaim_session_owned_scopes_for_chat(
    runtime_root: Path,
    chat_id: str,
    *,
    grace_seconds: float = 5.0,
    require_live_root: bool = False,
) -> list[CleanupResult]:
    """Reclaim all session_owned scopes on spawns belonging to a chat.

    Called at session exit. Terminates all unreleased session_owned scopes
    on spawns whose chat_id matches, regardless of which specific
    harness_session_id they reference.

    Crash repair passes ``require_live_root=True`` because it runs after a
    stale lease has been declared terminal; this keeps repair fail-closed when
    the recorded root is gone or the PID has been reused.
    """
    from meridian.lib.state.spawn_store import list_spawns

    spawns = list_spawns(runtime_root, filters={"chat_id": chat_id})
    results: list[CleanupResult] = []
    for record in spawns:
        spawn_id = SpawnId(record.id)
        scopes = read_scopes_from_disk(runtime_root, spawn_id)
        for scope in scopes:
            if scope.owner_policy != "session_owned":
                continue
            if is_scope_released(runtime_root, spawn_id, scope.release_id):
                continue
            if require_live_root:
                skip_reason = _live_root_skip_reason(scope)
                if skip_reason is not None:
                    result = CleanupResult(
                        scope_id=scope.scope_id,
                        root_pid=scope.root_pid,
                        descendant_count=None,
                        reason="session_exit",
                        grace_seconds=0.0,
                        kill_escalated=False,
                        degraded_fallback=False,
                        skip_reason=skip_reason,
                    )
                    logger.debug(
                        "Skipped session-owned scope reclaim during crash repair.",
                        spawn_id=record.id,
                        scope_id=scope.scope_id,
                        root_pid=scope.root_pid,
                        descendant_count=None,
                        reason="session_exit",
                        grace_seconds=0.0,
                        kill_escalated=False,
                        degraded_fallback=False,
                        skip_reason=skip_reason,
                        chat_id=chat_id,
                    )
                    results.append(result)
                    continue
            result = terminate_scope_sync(scope, grace_seconds=grace_seconds, reason="session_exit")
            mark_scope_released(runtime_root, spawn_id, scope.release_id)
            logger.debug(
                "Reclaimed session-owned scope at session exit.",
                spawn_id=record.id,
                scope_id=scope.scope_id,
                root_pid=scope.root_pid,
                descendant_count=result.descendant_count,
                reason="session_exit",
                grace_seconds=grace_seconds,
                kill_escalated=result.kill_escalated,
                degraded_fallback=result.degraded_fallback,
                skip_reason=result.skip_reason,
                chat_id=chat_id,
            )
            results.append(result)
    return results


def _live_root_skip_reason(scope: ProcessScopeSnapshot) -> str | None:
    if birth_time_unverified(scope.root_created_at_epoch):
        return "birth_time_unverified_skip_crash_reclaim_to_avoid_pid_reuse_kill"
    try:
        actual = psutil.Process(scope.root_pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return "root_not_alive"
    if abs(actual - scope.root_created_at_epoch) > 1.0:
        return "pid_reuse_detected"
    return None


__all__ = [
    "cancel_managed_primary",
    "reclaim_session_owned_scopes_for_chat",
    "should_skip_cleanup",
    "terminate_legacy_worker_fallback",
    "terminate_recorded_spawn_scopes",
    "terminate_spawn_scopes",
]
