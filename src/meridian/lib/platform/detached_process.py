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
import tempfile
import threading
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from typing import IO

import psutil
import structlog

from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.process_scope.base import (
    PROCESS_BIRTH_UNKNOWN_EPOCH,
    ProcessScopeSnapshot,
)

logger = structlog.get_logger(__name__)

_WATCHDOG_READY_TIMEOUT_SECONDS = 10.0
_WATCHDOG_STDERR_LIMIT_BYTES = 4096


@dataclass
class ParentWatchdog:
    """One launcher-owned POSIX watchdog accepting multiple process scopes."""

    process: subprocess.Popen[bytes]
    control_write_fd: int
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _closed: bool = False

    def register_scope(self, scope: ProcessScopeSnapshot) -> bool:
        """Commit one scope registration to the watchdog control pipe."""

        if scope.root_created_at_epoch == PROCESS_BIRTH_UNKNOWN_EPOCH:
            return False
        return self._send_message(
            {"action": "register", "scope": asdict(scope)},
        )

    def release_scope(self, release_id: str) -> bool:
        """Stop watching a scope after its owner has terminated and reaped it."""

        return self._send_message({"action": "release", "release_id": release_id})

    def _send_message(self, payload: dict[str, object]) -> bool:
        message = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
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
    watchdog: ParentWatchdog | None = None
    watchdog_release_id: str | None = None
    owns_watchdog: bool = False
    parent_death_linked: bool = False


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
    # The watchdog must not inherit Python startup controls from the launcher.
    # Settings such as PYTHONSAFEPATH remove the script directory from sys.path,
    # while PYTHONHOME (and other malformed PYTHON* values) can prevent the
    # interpreter from starting at all. Supply only the import path needed by
    # the isolated ``-S`` entrypoint.
    watchdog_env = {
        key: value for key, value in os.environ.items() if not key.startswith("PYTHON")
    }
    psutil_site_packages = os.path.dirname(os.path.dirname(psutil.__file__))
    watchdog_env["PYTHONPATH"] = os.pathsep.join(
        (os.path.dirname(__file__), psutil_site_packages)
    )
    try:
        stderr_capture = tempfile.TemporaryFile()  # noqa: SIM115  # closed in finally
    except OSError:
        for fd in (control_read_fd, control_write_fd, ready_read_fd, ready_write_fd):
            os.close(fd)
        logger.warning(
            "parent_watchdog.start_unavailable",
            platform=sys.platform,
            failure_reason="stderr_capture_unavailable",
            exc_info=True,
        )
        return None

    try:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_capture,
                close_fds=True,
                pass_fds=(control_read_fd, ready_write_fd),
                start_new_session=True,
                env=watchdog_env,
            )
        except OSError:
            for fd in (
                control_read_fd,
                control_write_fd,
                ready_read_fd,
                ready_write_fd,
            ):
                os.close(fd)
            logger.warning(
                "parent_watchdog.start_unavailable",
                platform=sys.platform,
                failure_reason="process_start_failed",
                exc_info=True,
            )
            return None

        os.close(control_read_fd)
        os.close(ready_write_fd)
        handshake_error = False
        try:
            ready_signal = _watchdog_ready_signal(ready_read_fd)
        except OSError:
            ready_signal = b""
            handshake_error = True
            logger.warning(
                "parent_watchdog.handshake_failed",
                platform=sys.platform,
                exc_info=True,
            )
        finally:
            os.close(ready_read_fd)

        watchdog = ParentWatchdog(process=process, control_write_fd=control_write_fd)
        if ready_signal == b"1":
            return watchdog

        returncode_before_cleanup = process.poll()
        watchdog.close()
        watchdog_stderr = _read_watchdog_stderr(stderr_capture)
        if handshake_error:
            logger.warning(
                "parent_watchdog.start_unavailable",
                platform=sys.platform,
                failure_reason="handshake_error",
                watchdog_returncode=process.returncode,
                watchdog_stderr=watchdog_stderr or None,
            )
        elif ready_signal is None:
            logger.warning(
                "parent_watchdog.start_unavailable",
                platform=sys.platform,
                failure_reason="readiness_timeout",
                timeout_seconds=_WATCHDOG_READY_TIMEOUT_SECONDS,
            )
        elif ready_signal == b"" or returncode_before_cleanup is not None:
            logger.warning(
                "parent_watchdog.start_unavailable",
                platform=sys.platform,
                failure_reason="early_exit",
                watchdog_returncode=process.returncode,
                watchdog_stderr=watchdog_stderr or None,
            )
        else:
            logger.warning(
                "parent_watchdog.start_unavailable",
                platform=sys.platform,
                failure_reason="invalid_ready_signal",
                watchdog_ready_signal=repr(ready_signal),
                watchdog_returncode=process.returncode,
                watchdog_stderr=watchdog_stderr or None,
            )
        return None
    finally:
        stderr_capture.close()


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
) -> ParentDeathLink:
    """Attach an already-started legacy Windows child to a Job Object."""

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


def release_parent_death_link(link: ParentDeathLink | None) -> None:
    """Release a dead scope and reap any watchdog owned by this link."""

    if link is None or link.watchdog is None:
        return
    if link.watchdog_release_id is not None:
        link.watchdog.release_scope(link.watchdog_release_id)
    if link.owns_watchdog:
        link.watchdog.close()


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


def _watchdog_ready_signal(read_fd: int) -> bytes | None:
    readable, _, _ = select.select(
        [read_fd],
        [],
        [],
        _WATCHDOG_READY_TIMEOUT_SECONDS,
    )
    if not readable:
        return None
    return os.read(read_fd, 1)


def _read_watchdog_stderr(stderr_capture: IO[bytes]) -> str:
    stderr_capture.seek(0)
    raw = stderr_capture.read(_WATCHDOG_STDERR_LIMIT_BYTES + 1)
    truncated = len(raw) > _WATCHDOG_STDERR_LIMIT_BYTES
    message = raw[:_WATCHDOG_STDERR_LIMIT_BYTES].decode("utf-8", errors="replace").strip()
    if truncated:
        message += "\n[truncated]"
    return message


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
    "ParentDeathLink",
    "ParentWatchdog",
    "link_child_lifetime_to_parent",
    "process_scope_for_parent_watchdog",
    "release_parent_death_link",
    "start_parent_watchdog",
]
