"""Core process-scope contracts: dataclasses and the ScopedProcessHandle wrapper."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256

import structlog

logger = structlog.get_logger(__name__)

PROCESS_BIRTH_UNKNOWN_EPOCH = 0.0


def birth_time_unverified(created_at_epoch: float) -> bool:
    """Return True when process birth time was not observed.

    The sentinel means "unknown", not "process born at epoch zero".
    Adapters must skip PID-reuse numeric comparisons in this state; callers
    decide whether an unknown birth time is safe for their policy layer.
    """

    return created_at_epoch == PROCESS_BIRTH_UNKNOWN_EPOCH


@dataclass(frozen=True)
class ProcessScopeSnapshot:
    """Mechanism facts for a single process scope (no policy)."""

    scope_id: str
    """Human-readable label, e.g. 'backend', 'tui', 'worker'."""

    owner_policy: str
    """'spawn_owned' | 'session_owned'."""

    owner_id: str
    """spawn_id or session_id that owns this scope."""

    role: str
    """'harness_backend' | 'tool_worker' | etc."""

    containment: str
    """'posix_pgid' | 'windows_job' | 'pid_tree_fallback'."""

    root_pid: int
    """PID of the scope root process."""

    root_created_at_epoch: float
    """Birth time in epoch seconds — PID reuse guard (PROC-006)."""

    pgid: int | None
    """POSIX process group ID; None on Windows or when containment != 'posix_pgid'."""

    job_name: str | None
    """Windows Job Object name; None on POSIX or when containment != 'windows_job'."""

    degraded_reason: str | None
    """Set when containment fell back from the preferred mechanism."""

    parent_death_linked: bool = False
    """True when the scope root is linked to the launcher lifetime by platform support."""

    release_id: str = ""
    """Stable identity for this concrete scope release."""

    def __post_init__(self) -> None:
        if not self.release_id:
            object.__setattr__(
                self,
                "release_id",
                process_scope_release_id(
                    scope_id=self.scope_id,
                    root_pid=self.root_pid,
                    root_created_at_epoch=self.root_created_at_epoch,
                ),
            )


def process_scope_release_id(
    *,
    scope_id: str,
    root_pid: int,
    root_created_at_epoch: float,
) -> str:
    """Build a stable release identity for one concrete process scope."""

    seed = f"{scope_id}:{root_pid}:{root_created_at_epoch:.6f}"
    return f"{scope_id}:{sha256(seed.encode('utf-8')).hexdigest()[:16]}"


@dataclass(frozen=True)
class CleanupResult:
    """Outcome record from a process-scope termination attempt."""

    scope_id: str
    root_pid: int
    descendant_count: int | None
    reason: str
    """'stop_called' | 'reaper' | 'cancel' | etc."""

    grace_seconds: float
    kill_escalated: bool
    degraded_fallback: bool
    skip_reason: str | None
    """Set when cleanup was intentionally skipped (e.g. 'pid_reuse_detected')."""


class ScopedProcessHandle:
    """Wrap an asyncio subprocess and its ProcessScopeSnapshot.

    Delegates termination to the appropriate platform backend based on
    ``snapshot.containment``.
    """

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        snapshot: ProcessScopeSnapshot,
    ) -> None:
        self._process = process
        self._snapshot = snapshot

    @property
    def process(self) -> asyncio.subprocess.Process:
        return self._process

    @property
    def snapshot(self) -> ProcessScopeSnapshot:
        return self._snapshot

    @property
    def pid(self) -> int:
        return self._process.pid

    async def terminate(
        self,
        grace_seconds: float = 5.0,
        reason: str = "stop_called",
    ) -> CleanupResult:
        """Terminate the process scope and return a structured result.

        Delegates to the correct platform adapter based on ``snapshot.containment``.
        Emits a structlog event with PROC-011 fields after completion.
        Never raises — termination failures are returned as degraded CleanupResult.
        """
        snap = self._snapshot
        result: CleanupResult

        try:
            if snap.containment == "posix_pgid":
                from meridian.lib.platform.process_scope.posix import terminate_pgid

                if snap.pgid is None:
                    # Degenerate case: containment says posix_pgid but pgid is missing.
                    result = await _fallback_terminate(self._process, snap, grace_seconds, reason)
                else:
                    result = terminate_pgid(
                        pgid=snap.pgid,
                        root_pid=snap.root_pid,
                        created_at_epoch=snap.root_created_at_epoch,
                        grace_seconds=grace_seconds,
                        reason=reason,
                        scope_id=snap.scope_id,
                    )

            elif snap.containment == "windows_job":
                # job_name is stored; the handle must be kept alive by the caller.
                # At this layer the handle is not yet threaded through, so we fall
                # back to tree termination and mark degraded_fallback.  A later
                # subphase will wire the handle through ScopedProcessHandle.
                result = await _fallback_terminate(
                    self._process, snap, grace_seconds, reason, degraded=True
                )

            else:
                # 'pid_tree_fallback' or unknown
                result = await _fallback_terminate(self._process, snap, grace_seconds, reason)
        except Exception:
            logger.warning(
                "process_scope.terminate_failed",
                spawn_id=snap.owner_id,
                scope_id=snap.scope_id,
                root_pid=snap.root_pid,
                reason=reason,
                exc_info=True,
            )
            result = CleanupResult(
                scope_id=snap.scope_id,
                root_pid=snap.root_pid,
                descendant_count=None,
                reason=reason,
                grace_seconds=grace_seconds,
                kill_escalated=False,
                degraded_fallback=True,
                skip_reason="termination_exception",
            )

        _emit_termination_event(snap, result)
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fallback_terminate(
    process: asyncio.subprocess.Process,
    snap: ProcessScopeSnapshot,
    grace_seconds: float,
    reason: str,
    *,
    degraded: bool = False,
) -> CleanupResult:
    from meridian.lib.platform.process_scope.fallback import terminate_tree

    return await terminate_tree(
        process,
        grace_secs=grace_seconds,
        reason=reason,
        scope_id=snap.scope_id,
        root_created_at_epoch=snap.root_created_at_epoch,
        degraded_fallback=degraded or snap.containment != "pid_tree_fallback",
    )


def _emit_termination_event(
    snap: ProcessScopeSnapshot,
    result: CleanupResult,
) -> None:
    logger.info(
        "process_scope.terminated",
        spawn_id=snap.owner_id,
        scope_id=result.scope_id,
        root_pid=result.root_pid,
        descendant_count=result.descendant_count,
        reason=result.reason,
        grace_seconds=result.grace_seconds,
        kill_escalated=result.kill_escalated,
        degraded_fallback=result.degraded_fallback,
        skip_reason=result.skip_reason,
    )


__all__ = [
    "PROCESS_BIRTH_UNKNOWN_EPOCH",
    "CleanupResult",
    "ProcessScopeSnapshot",
    "ScopedProcessHandle",
    "birth_time_unverified",
    "process_scope_release_id",
]
