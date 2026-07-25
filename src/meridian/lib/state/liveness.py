"""Process liveness probes."""

import time
from pathlib import Path

import psutil

from meridian.lib.platform.process_scope.base import birth_time_unverified

_PID_REUSE_GUARD_SECS = 30.0


def _is_running_non_zombie(proc: psutil.Process) -> bool:
    """Return process liveness suitable for cancellation convergence."""

    return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE


def is_process_alive(pid: int, created_after_epoch: float | None = None) -> bool:
    """Check if a PID is alive, with create_time guard for PID reuse."""
    if not psutil.pid_exists(pid):
        return False

    try:
        proc = psutil.Process(pid)
        # Process created after the tracked start time is PID reuse.
        if (
            created_after_epoch is not None
            and proc.create_time() > created_after_epoch + _PID_REUSE_GUARD_SECS
        ):
            return False
        return _is_running_non_zombie(proc)
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


def is_process_alive_with_birth(pid: int, birth_epoch: float | None) -> bool:
    """Alive AND exact-birth match (PID-reuse safe). Fail closed when birth is unknown."""

    if birth_epoch is None or birth_time_unverified(birth_epoch):
        return False
    if not psutil.pid_exists(pid):
        return False

    try:
        proc = psutil.Process(pid)
        if abs(proc.create_time() - birth_epoch) > 1.0:
            return False
        return _is_running_non_zombie(proc)
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return False
    except OSError:
        return False


def is_spawn_genuinely_active(runtime_root: Path, spawn_id: str) -> bool:
    """Read-only check: is this spawn genuinely active?

    Uses spawn store status + heartbeat freshness + runner PID liveness. Does not
    mutate spawn state; stale-active repair is left to the reaper.
    """
    from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
    from meridian.lib.state.spawn_store import get_spawn

    record = get_spawn(runtime_root, spawn_id)
    if record is None:
        return False

    if not is_active_spawn_status(record.status):
        return False

    if (
        record.runner_pid is not None
        and record.runner_pid > 0
        and is_process_alive(record.runner_pid)
    ):
        return True

    heartbeat_path = runtime_root / "spawns" / spawn_id / "heartbeat"
    try:
        mtime = heartbeat_path.stat().st_mtime
        if time.time() - mtime < 120:
            return True
    except OSError:
        pass

    return False
