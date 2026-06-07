"""Parent-death linkage for detached subprocess backends."""

from __future__ import annotations

import os
import signal
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from meridian.lib.platform import IS_WINDOWS

_PR_SET_PDEATHSIG = 1


@dataclass(frozen=True)
class ParentDeathLink:
    """Platform handle that keeps a child tied to this process lifetime."""

    job_name: str | None = None
    job_handle: object | None = None


def detached_backend_subprocess_kwargs() -> dict[str, Any]:
    """Return subprocess kwargs for detached harness backends.

    POSIX backends start in their own session and receive SIGKILL when this
    process dies. Windows backends are linked after spawn with a Job Object.
    """

    if IS_WINDOWS:
        return {}
    return {
        "start_new_session": True,
        "preexec_fn": _posix_parent_death_preexec,
    }


def link_child_lifetime_to_parent(pid: int) -> ParentDeathLink:
    """Attach an already-started child to this process lifetime when needed."""

    if not IS_WINDOWS:
        return ParentDeathLink()

    from meridian.lib.platform.process_scope.windows_job import assign_to_new_job

    result = assign_to_new_job(pid)
    if result is None:
        return ParentDeathLink()
    job_name, job_handle = result
    return ParentDeathLink(job_name=job_name, job_handle=job_handle)


def _posix_parent_death_preexec() -> None:
    _set_parent_death_signal(signal.SIGKILL)
    if os.getppid() == 1:
        os.kill(os.getpid(), signal.SIGKILL)


def _set_parent_death_signal(signum: signal.Signals) -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    prctl: Callable[..., int] | None = getattr(libc, "prctl", None)
    if prctl is None:
        return
    result = prctl(_PR_SET_PDEATHSIG, int(signum))
    if result != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))


__all__ = [
    "ParentDeathLink",
    "detached_backend_subprocess_kwargs",
    "link_child_lifetime_to_parent",
]
