"""Process liveness probes."""

import psutil

from meridian.lib.platform.process_scope.base import birth_time_unverified

_PID_REUSE_GUARD_SECS = 30.0
_BIRTH_TOLERANCE_SECS = 1.0


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


def _process_alive_with_birth(pid: int | None, birth_epoch: float | None) -> bool | None:
    """Return matching liveness, or unknown when identity cannot be checked."""
    if pid is None or pid <= 0 or birth_epoch is None or birth_time_unverified(birth_epoch):
        return None
    try:
        proc = psutil.Process(pid)
        # psutil's epoch birth can move with wall-clock corrections.
        if abs(proc.create_time() - birth_epoch) > _BIRTH_TOLERANCE_SECS:
            return False
        return _is_running_non_zombie(proc)
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, OSError):
        return None


def is_process_alive_with_birth(pid: int, birth_epoch: float | None) -> bool:
    """Alive AND exact-birth match (PID-reuse safe). Unknown is not alive."""
    return _process_alive_with_birth(pid, birth_epoch) is True


def is_process_dead_with_birth(pid: int | None, birth_epoch: float | None) -> bool:
    """Inverse of known birth-matched liveness; inaccessible/unknown is not dead."""
    return _process_alive_with_birth(pid, birth_epoch) is False
