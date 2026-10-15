"""Parent-death linkage for subprocess children.

Platform contract:
- **Linux** with ``prctl(PR_SET_PDEATHSIG)``: child receives SIGKILL when this
  process dies (installed in a pre-exec hook before the child execs).
- **Other POSIX** (macOS, BSD, Linux without ``prctl``): a tiny detached
  watchdog process terminates either the child's isolated process group or its
  PID-guarded process tree if the Meridian launcher dies first.
- **Windows**: parent-death linkage is applied post-spawn via a Job Object
  (see ``link_child_lifetime_to_parent``).
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psutil
import structlog

from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.process_scope.base import PROCESS_BIRTH_UNKNOWN_EPOCH

logger = structlog.get_logger(__name__)

_PR_SET_PDEATHSIG = 1
_WATCHDOG_READY_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class DetachedSubprocessConfig:
    """Subprocess options and pre-exec parent-death containment capability."""

    kwargs: dict[str, Any]
    parent_death_linked: bool


@dataclass(frozen=True)
class ParentDeathLink:
    """Platform handle that keeps a child tied to this process lifetime."""

    job_name: str | None = None
    job_handle: object | None = None
    watchdog_process: subprocess.Popen[bytes] | None = None
    parent_death_linked: bool = False


def detached_subprocess_config() -> DetachedSubprocessConfig:
    """Return subprocess options and whether pre-exec linkage will be installed.

    On Linux with ``prctl``, ``parent_death_linked`` is True and the returned
    kwargs include a pre-exec hook that sets ``PR_SET_PDEATHSIG``. Other POSIX
    platforms still get ``start_new_session`` so the later watchdog / cleanup
    path can target a dedicated process group. Windows returns empty kwargs;
    linkage happens post-spawn.
    """

    return parent_death_subprocess_config(start_new_session=True)


def parent_death_subprocess_config(
    *,
    start_new_session: bool,
    child_preexec_fn: Callable[[], None] | None = None,
) -> DetachedSubprocessConfig:
    """Build subprocess kwargs with parent-death linkage independent of sessions.

    ``start_new_session=False`` is used by native terminal children, which must
    remain in the invoking shell's foreground process group. An optional child
    setup hook is composed with Linux ``PR_SET_PDEATHSIG`` in the one pre-exec
    callback so callers do not duplicate the platform mechanism.
    """

    if IS_WINDOWS:
        return DetachedSubprocessConfig(kwargs={}, parent_death_linked=False)

    preexec_steps: list[Callable[[], None]] = []
    parent_death_linked = _linux_parent_death_sig_available()
    if parent_death_linked:
        preexec_steps.append(_posix_parent_death_preexec)
    if child_preexec_fn is not None:
        preexec_steps.append(child_preexec_fn)

    kwargs: dict[str, Any] = {"start_new_session": start_new_session}
    if preexec_steps:
        kwargs["preexec_fn"] = _compose_preexec(tuple(preexec_steps))
    return DetachedSubprocessConfig(
        kwargs=kwargs,
        parent_death_linked=parent_death_linked,
    )


def link_child_lifetime_to_parent(
    pid: int,
    *,
    target_process_group: bool = True,
    scope_id: str = "backend",
    role: str = "harness_backend",
) -> ParentDeathLink:
    """Attach an already-started child to this process lifetime when needed."""

    if IS_WINDOWS:
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

    return _start_parent_watchdog(
        pid,
        target_process_group=target_process_group,
        scope_id=scope_id,
        role=role,
    )


def release_parent_death_link(link: ParentDeathLink | None) -> None:
    """Release/reap any helper resources associated with a parent-death link."""

    if link is None or link.watchdog_process is None:
        return
    try:
        link.watchdog_process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        link.watchdog_process.terminate()
        try:
            link.watchdog_process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            link.watchdog_process.kill()
            link.watchdog_process.wait(timeout=1.0)


def _start_parent_watchdog(
    pid: int,
    *,
    target_process_group: bool,
    scope_id: str,
    role: str,
) -> ParentDeathLink:
    parent_pid = os.getpid()
    parent_created_at = _process_birth_epoch(parent_pid)
    target_created_at = _process_birth_epoch(pid)
    target_pgid = _process_group_id(pid) if target_process_group else None

    try:
        ready_read_fd, ready_write_fd = os.pipe()
    except OSError:
        logger.warning(
            "detached_backend_parent_watchdog_unavailable",
            platform=sys.platform,
            target_pid=pid,
            exc_info=True,
        )
        return ParentDeathLink(parent_death_linked=False)
    command = (
        sys.executable,
        "-m",
        "meridian.lib.platform.parent_watchdog",
        "--parent-pid",
        str(parent_pid),
        "--parent-created-at",
        str(parent_created_at),
        "--target-pid",
        str(pid),
        "--target-created-at",
        str(target_created_at),
        "--scope-id",
        scope_id,
        "--role",
        role,
        "--ready-fd",
        str(ready_write_fd),
    )
    if target_pgid is not None:
        command = (*command, "--target-pgid", str(target_pgid))
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(ready_write_fd,),
            start_new_session=True,
        )
    except OSError:
        os.close(ready_read_fd)
        os.close(ready_write_fd)
        logger.warning(
            "detached_backend_parent_watchdog_unavailable",
            platform=sys.platform,
            target_pid=pid,
            exc_info=True,
        )
        return ParentDeathLink(parent_death_linked=False)
    os.close(ready_write_fd)

    try:
        ready = _watchdog_reported_ready(ready_read_fd)
    except OSError:
        ready = False
        logger.warning(
            "detached_backend_parent_watchdog_handshake_failed",
            platform=sys.platform,
            target_pid=pid,
            exc_info=True,
        )
    finally:
        os.close(ready_read_fd)
    if not ready:
        returncode = process.poll()
        release_parent_death_link(ParentDeathLink(watchdog_process=process))
        logger.warning(
            "detached_backend_parent_watchdog_unavailable",
            platform=sys.platform,
            target_pid=pid,
            watchdog_returncode=returncode,
        )
        return ParentDeathLink(parent_death_linked=False)

    return ParentDeathLink(
        watchdog_process=process,
        parent_death_linked=True,
    )


def _watchdog_reported_ready(read_fd: int) -> bool:
    readable, _, _ = select.select(
        [read_fd],
        [],
        [],
        _WATCHDOG_READY_TIMEOUT_SECONDS,
    )
    return bool(readable and os.read(read_fd, 1) == b"1")


def _compose_preexec(steps: tuple[Callable[[], None], ...]) -> Callable[[], None]:
    def _run() -> None:
        for step in steps:
            step()

    return _run


def _process_birth_epoch(pid: int) -> float:
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return PROCESS_BIRTH_UNKNOWN_EPOCH


def _process_group_id(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except OSError:
        return None


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
    "parent_death_subprocess_config",
    "release_parent_death_link",
]
