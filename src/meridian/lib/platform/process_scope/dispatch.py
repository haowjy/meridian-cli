"""Containment-aware termination dispatch."""

from __future__ import annotations

import structlog

from meridian.lib.platform.process_scope.base import CleanupResult, ProcessScopeSnapshot
from meridian.lib.platform.process_scope.fallback import terminate_tree_sync
from meridian.lib.platform.process_scope.posix import terminate_pgid

logger = structlog.get_logger(__name__)


def terminate_scope_sync(
    scope: ProcessScopeSnapshot,
    *,
    grace_seconds: float,
    reason: str,
) -> CleanupResult:
    """Synchronous containment-aware termination dispatch.

    Counterpart to ScopedProcessHandle.terminate() for sync callers
    (reaper, cancel, session-exit).  Never raises — termination failures
    are returned as a degraded CleanupResult.
    """
    try:
        if scope.containment == "posix_pgid" and scope.pgid is not None:
            return terminate_pgid(
                pgid=scope.pgid,
                root_pid=scope.root_pid,
                created_at_epoch=scope.root_created_at_epoch,
                grace_seconds=grace_seconds,
                reason=reason,
                scope_id=scope.scope_id,
            )
        return terminate_tree_sync(
            pid=scope.root_pid,
            created_at_epoch=scope.root_created_at_epoch,
            grace_secs=grace_seconds,
            reason=reason,
            scope_id=scope.scope_id,
            degraded_fallback=scope.containment != "pid_tree_fallback",
        )
    except Exception:
        logger.warning(
            "terminate_scope_sync.failed",
            scope_id=scope.scope_id,
            root_pid=scope.root_pid,
            reason=reason,
            exc_info=True,
        )
        return CleanupResult(
            scope_id=scope.scope_id,
            root_pid=scope.root_pid,
            descendant_count=None,
            reason=reason,
            grace_seconds=grace_seconds,
            kill_escalated=False,
            degraded_fallback=True,
            skip_reason="termination_exception",
        )
