"""Parent-death containment for launcher-owned subprocess trees.

POSIX launchers use one detached watchdog process. The watchdog starts before
children and receives each backend or TUI scope over a pipe, so wrapper
processes and their descendants share one crash boundary. Windows continues to
use a Job Object assigned after process creation.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from typing import Any

import psutil
import structlog

from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.process_scope.base import (
    PROCESS_BIRTH_UNKNOWN_EPOCH,
    ProcessScopeSnapshot,
)

logger = structlog.get_logger(__name__)

_WATCHDOG_READY_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class DetachedSubprocessConfig:
    """Subprocess options for an isolated managed backend."""

    kwargs: dict[str, Any]


@dataclass
class ParentWatchdog:
    """One launcher-owned POSIX watchdog accepting multiple process scopes."""

    process: subprocess.Popen[bytes]
    control_write_fd: int
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _closed: bool = False

    @property
    def rss_bytes(self) -> int | None:
        try:
            return psutil.Process(self.process.pid).memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return None

    def register_scope(self, scope: ProcessScopeSnapshot) -> bool:
        """Commit one scope registration to the watchdog control pipe."""

        message = (
            json.dumps(
                {"action": "register", "scope": asdict(scope)},
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        with self._lock:
            if self._closed or self.process.poll() is not None:
                return False
            try:
                os.write(self.control_write_fd, message)
            except OSError:
                return False
        return True

    def close(self) -> None:
        """Gracefully release the watchdog without terminating registered scopes."""

        with self._lock:
            if not self._closed:
                self._closed = True
                with suppress(OSError):
                    os.write(self.control_write_fd, b'{"action":"shutdown"}\n')
                with suppress(OSError):
                    os.close(self.control_write_fd)
        _reap_watchdog_process(self.process)


@dataclass(frozen=True)
class ParentDeathLink:
    """Platform handle tying a process scope to its launcher's lifetime."""

    job_name: str | None = None
    job_handle: object | None = None
    owned_watchdog: ParentWatchdog | None = None
    parent_death_linked: bool = False

    @property
    def watchdog_process(self) -> subprocess.Popen[bytes] | None:
        watchdog = self.owned_watchdog
        return watchdog.process if watchdog is not None else None


def detached_subprocess_config() -> DetachedSubprocessConfig:
    """Return backend isolation options; parent death is linked post-spawn."""

    if IS_WINDOWS:
        return DetachedSubprocessConfig(kwargs={})
    return DetachedSubprocessConfig(kwargs={"start_new_session": True})


def start_parent_watchdog() -> ParentWatchdog | None:
    """Start an empty launcher watchdog and wait until it can accept scopes."""

    if IS_WINDOWS:
        return None

    parent_pid = os.getpid()
    parent_created_at = _process_birth_epoch(parent_pid)
    try:
        control_read_fd, control_write_fd = os.pipe()
    except OSError:
        logger.warning(
            "parent_watchdog.pipe_unavailable",
            platform=sys.platform,
            exc_info=True,
        )
        return None
    try:
        ready_read_fd, ready_write_fd = os.pipe()
    except OSError:
        os.close(control_read_fd)
        os.close(control_write_fd)
        logger.warning(
            "parent_watchdog.pipe_unavailable",
            platform=sys.platform,
            exc_info=True,
        )
        return None

    command = (
        sys.executable,
        "-S",
        "-OO",
        os.path.join(os.path.dirname(__file__), "parent_watchdog.py"),
        "--parent-pid",
        str(parent_pid),
        "--parent-created-at",
        str(parent_created_at),
        "--control-fd",
        str(control_read_fd),
        "--ready-fd",
        str(ready_write_fd),
    )
    watchdog_env = os.environ.copy()
    psutil_site_packages = os.path.dirname(os.path.dirname(psutil.__file__))
    watchdog_env["PYTHONPATH"] = psutil_site_packages
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(control_read_fd, ready_write_fd),
            start_new_session=True,
            env=watchdog_env,
        )
    except OSError:
        for fd in (control_read_fd, control_write_fd, ready_read_fd, ready_write_fd):
            os.close(fd)
        logger.warning(
            "parent_watchdog.start_unavailable",
            platform=sys.platform,
            exc_info=True,
        )
        return None

    os.close(control_read_fd)
    os.close(ready_write_fd)
    try:
        ready = _watchdog_reported_ready(ready_read_fd)
    except OSError:
        ready = False
        logger.warning(
            "parent_watchdog.handshake_failed",
            platform=sys.platform,
            exc_info=True,
        )
    finally:
        os.close(ready_read_fd)

    watchdog = ParentWatchdog(process=process, control_write_fd=control_write_fd)
    if ready:
        return watchdog

    watchdog.close()
    logger.warning(
        "parent_watchdog.start_unavailable",
        platform=sys.platform,
        watchdog_returncode=process.returncode,
    )
    return None


def process_scope_for_parent_watchdog(
    pid: int,
    *,
    target_process_group: bool,
    scope_id: str,
    role: str,
    owner_id: str = "parent-watchdog",
) -> ProcessScopeSnapshot:
    """Capture a PID/birth-guarded scope safe for watchdog termination."""

    target_pgid = _process_group_id(pid) if target_process_group else None
    if target_pgid != pid:
        target_pgid = None
    return ProcessScopeSnapshot(
        scope_id=scope_id,
        owner_policy="spawn_owned",
        owner_id=owner_id,
        role=role,
        containment="posix_pgid" if target_pgid is not None else "pid_tree_fallback",
        root_pid=pid,
        root_created_at_epoch=_process_birth_epoch(pid),
        pgid=target_pgid,
        job_name=None,
        degraded_reason=None,
        parent_death_linked=True,
    )


def link_child_lifetime_to_parent(
    pid: int,
    *,
    target_process_group: bool = True,
    scope_id: str = "backend",
    role: str = "harness_backend",
) -> ParentDeathLink:
    """Attach an already-started child to this process lifetime."""

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

    watchdog = start_parent_watchdog()
    if watchdog is None:
        return ParentDeathLink(parent_death_linked=False)
    scope = process_scope_for_parent_watchdog(
        pid,
        target_process_group=target_process_group,
        scope_id=scope_id,
        role=role,
    )
    if not watchdog.register_scope(scope):
        watchdog.close()
        return ParentDeathLink(parent_death_linked=False)
    return ParentDeathLink(
        owned_watchdog=watchdog,
        parent_death_linked=True,
    )


def release_parent_death_link(link: ParentDeathLink | None) -> None:
    """Release/reap helper resources owned by a parent-death link."""

    if link is not None and link.owned_watchdog is not None:
        link.owned_watchdog.close()


def _reap_watchdog_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1.0)


def _watchdog_reported_ready(read_fd: int) -> bool:
    readable, _, _ = select.select(
        [read_fd],
        [],
        [],
        _WATCHDOG_READY_TIMEOUT_SECONDS,
    )
    return bool(readable and os.read(read_fd, 1) == b"1")


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


__all__ = [
    "DetachedSubprocessConfig",
    "ParentDeathLink",
    "ParentWatchdog",
    "detached_subprocess_config",
    "link_child_lifetime_to_parent",
    "process_scope_for_parent_watchdog",
    "release_parent_death_link",
    "start_parent_watchdog",
]
