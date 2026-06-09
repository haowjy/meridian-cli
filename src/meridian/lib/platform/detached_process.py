"""Parent-death linkage for detached subprocess backends.

Platform contract:
- **Linux** with ``prctl(PR_SET_PDEATHSIG)``: child receives SIGKILL when this
  process dies (installed in a pre-exec hook before the child execs).
- **Other POSIX** (macOS, BSD, etc.): no kernel parent-death signal exists;
  detached backends start in a new session only — callers must treat
  ``parent_death_linked=False`` as degraded containment.
- **Windows**: parent-death linkage is applied post-spawn via a Job Object
  (see ``link_child_lifetime_to_parent``).
"""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog

from meridian.lib.platform import IS_WINDOWS

logger = structlog.get_logger(__name__)

_PR_SET_PDEATHSIG = 1


@dataclass(frozen=True)
class DetachedSubprocessConfig:
    """Subprocess options and parent-death containment capability."""

    kwargs: dict[str, Any]
    parent_death_linked: bool


@dataclass(frozen=True)
class ParentDeathLink:
    """Platform handle that keeps a child tied to this process lifetime."""

    job_name: str | None = None
    job_handle: object | None = None
    parent_death_linked: bool = False


def detached_subprocess_config() -> DetachedSubprocessConfig:
    """Return subprocess options and whether parent-death linkage will be installed.

    On Linux with ``prctl``, ``parent_death_linked`` is True and the returned
    kwargs include a pre-exec hook that sets ``PR_SET_PDEATHSIG``. On other
    POSIX platforms only ``start_new_session`` is applied and a structured
    warning is logged. Windows returns empty kwargs; linkage happens post-spawn.
    """

    if IS_WINDOWS:
        return DetachedSubprocessConfig(kwargs={}, parent_death_linked=False)

    if _linux_parent_death_sig_available():
        return DetachedSubprocessConfig(
            kwargs={
                "start_new_session": True,
                "preexec_fn": _posix_parent_death_preexec,
            },
            parent_death_linked=True,
        )

    logger.warning(
        "detached_backend_parent_death_unavailable",
        platform=sys.platform,
        containment="start_new_session_only",
    )
    return DetachedSubprocessConfig(
        kwargs={"start_new_session": True},
        parent_death_linked=False,
    )


def link_child_lifetime_to_parent(pid: int) -> ParentDeathLink:
    """Attach an already-started child to this process lifetime when needed."""

    if not IS_WINDOWS:
        return ParentDeathLink(parent_death_linked=False)

    from meridian.lib.platform.process_scope.windows_job import assign_to_new_job

    result = assign_to_new_job(pid)
    if result is None:
        return ParentDeathLink(parent_death_linked=False)
    job_name, job_handle = result
    return ParentDeathLink(
        job_name=job_name,
        job_handle=job_handle,
        parent_death_linked=True,
    )


def _linux_parent_death_sig_available() -> bool:
    if sys.platform != "linux":
        return False
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    return getattr(libc, "prctl", None) is not None


def _posix_parent_death_preexec() -> None:
    _set_parent_death_signal(signal.SIGKILL)
    if os.getppid() == 1:
        os.kill(os.getpid(), signal.SIGKILL)


def _set_parent_death_signal(signum: signal.Signals) -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    prctl: Callable[..., int] | None = getattr(libc, "prctl", None)
    if prctl is None:
        raise RuntimeError("prctl unavailable in preexec despite capability probe")
    result = prctl(_PR_SET_PDEATHSIG, int(signum))
    if result != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))


__all__ = [
    "DetachedSubprocessConfig",
    "ParentDeathLink",
    "detached_subprocess_config",
    "link_child_lifetime_to_parent",
]
