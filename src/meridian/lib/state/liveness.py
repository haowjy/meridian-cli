"""Process liveness probes."""

import math
import os
from contextlib import suppress

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


def signal_with_birth(
    pid: int | None, birth_epoch: float | None, sig: int, *, tree: bool = False
) -> bool:
    """Signal a process only when its exact birth matches; with ``tree``, its descendants too.

    A PID whose birth is unknown or differs is never signalled. Returns whether a signal
    was sent.
    """
    if pid is None or pid == os.getpid() or not is_process_alive_with_birth(pid, birth_epoch):
        return False
    try:
        root = psutil.Process(pid)
        targets = [*root.children(recursive=True), root] if tree else [root]
    except psutil.Error:
        return False
    for proc in targets:
        with suppress(psutil.Error):
            proc.send_signal(sig)
    return True


def is_process_alive_born_by(pid: int, epoch: float | None) -> bool:
    """Return whether a live non-zombie PID was born no later than *epoch*."""

    if pid <= 0 or epoch is None or not math.isfinite(epoch) or epoch <= 0:
        return False
    try:
        proc = psutil.Process(pid)
        created_at = proc.create_time()
        if not math.isfinite(created_at) or created_at <= 0:
            return False
        return created_at <= epoch + _BIRTH_TOLERANCE_SECS and _is_running_non_zombie(proc)
    except (psutil.Error, OSError):
        return False


def is_process_dead_with_birth(pid: int | None, birth_epoch: float | None) -> bool:
    """Inverse of known birth-matched liveness; inaccessible/unknown is not dead."""
    return _process_alive_with_birth(pid, birth_epoch) is False
