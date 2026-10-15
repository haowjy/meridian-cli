"""POSIX process-group scope adapter.

The implementation lives in the dependency-free termination module so the
standalone parent watchdog and Meridian use exactly the same kill mechanics.
"""

from __future__ import annotations

from meridian.lib.platform import scope_termination
from meridian.lib.platform.process_scope.base import CleanupResult

is_pgid_reachable = scope_termination.is_pgid_reachable


def terminate_pgid(
    pgid: int,
    root_pid: int,
    created_at_epoch: float,
    grace_seconds: float,
    reason: str,
    scope_id: str,
) -> CleanupResult:
    """Terminate a POSIX process group and return Meridian's result record."""

    outcome = scope_termination.terminate_pgid(
        pgid=pgid,
        root_pid=root_pid,
        created_at_epoch=created_at_epoch,
        grace_seconds=grace_seconds,
    )
    return CleanupResult(
        scope_id=scope_id,
        root_pid=root_pid,
        descendant_count=outcome.descendant_count,
        reason=reason,
        grace_seconds=grace_seconds,
        kill_escalated=outcome.kill_escalated,
        degraded_fallback=outcome.degraded_fallback,
        skip_reason=outcome.skip_reason,
    )


__all__ = ["is_pgid_reachable", "terminate_pgid"]
