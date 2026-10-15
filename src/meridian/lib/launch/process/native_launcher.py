"""Native terminal process launcher with launcher-owned lifecycle guarantees."""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.detached_process import (
    ParentDeathLink,
    link_child_lifetime_to_parent,
    parent_death_subprocess_config,
    release_parent_death_link,
)

from .ports import PRIMARY_STDERR_LOG_PATH_ENV, LaunchedProcess, ProcessLauncher

logger = structlog.get_logger(__name__)


def can_use_native_terminal() -> bool:
    """Return whether this process has the POSIX terminal native mode requires."""

    return not IS_WINDOWS and sys.stdin.isatty() and sys.stdout.isatty()


def _reset_child_sigint() -> None:
    """Undo the launcher's temporary SIGINT ignore before child exec."""

    signal.signal(signal.SIGINT, signal.SIG_DFL)


@dataclass
class _RunningNativeProcess:
    process: subprocess.Popen[str]
    parent_death_link: ParentDeathLink
    restore_parent_sigint: Callable[[], None]
    parent_loop: asyncio.AbstractEventLoop | None
    wait_cancelled: threading.Event = field(default_factory=threading.Event)
    _restore_lock: threading.Lock = field(default_factory=threading.Lock)
    _restore_scheduled: bool = False

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def parent_death_linked(self) -> bool:
        return self.parent_death_link.parent_death_linked

    def terminate(self) -> None:
        if self.process.poll() is None:
            with suppress(ProcessLookupError):
                self.process.terminate()

    def cancel_wait(self) -> None:
        """Cancel the wait and ensure the inherited TUI cannot remain running."""

        self.wait_cancelled.set()
        self.terminate()

    def wait(self) -> LaunchedProcess:
        try:
            while True:
                if self.wait_cancelled.is_set():
                    self._reap_cancelled_process()
                    return LaunchedProcess(exit_code=130, pid=self.process.pid)
                try:
                    exit_code = self.process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    continue
                return LaunchedProcess(exit_code=exit_code, pid=self.process.pid)
        finally:
            release_parent_death_link(self.parent_death_link)
            self._schedule_parent_sigint_restore()

    def _reap_cancelled_process(self) -> None:
        self.terminate()
        try:
            self.process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()

    def _schedule_parent_sigint_restore(self) -> None:
        with self._restore_lock:
            if self._restore_scheduled:
                return
            self._restore_scheduled = True

        if threading.current_thread() is threading.main_thread():
            self.restore_parent_sigint()
            return
        if self.parent_loop is not None:
            try:
                self.parent_loop.call_soon_threadsafe(self.restore_parent_sigint)
                return
            except RuntimeError:
                pass
        logger.error(
            "native_launcher.sigint_restore_unavailable",
            child_pid=self.process.pid,
        )


class NativeProcessLauncher(ProcessLauncher):
    """Launch a TUI on inherited stdio while tying its lifetime to Meridian."""

    def start(
        self,
        *,
        command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        output_log_path: Path | None,
    ) -> _RunningNativeProcess:
        if IS_WINDOWS:
            raise RuntimeError("Native POSIX terminal launcher is not available on Windows")
        if output_log_path is not None:
            raise ValueError("Native terminal launcher does not capture output")
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Native terminal child must be forked from the main thread")

        try:
            parent_loop = asyncio.get_running_loop()
        except RuntimeError:
            parent_loop = None

        previous_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal.SIG_IGN)

        def _restore_parent_sigint() -> None:
            signal.signal(signal.SIGINT, previous_sigint)

        subprocess_config = parent_death_subprocess_config(
            start_new_session=False,
            child_preexec_fn=_reset_child_sigint,
        )
        child_env = dict(env)
        child_env.pop(PRIMARY_STDERR_LOG_PATH_ENV, None)
        try:
            process: subprocess.Popen[str] = subprocess.Popen(
                command,
                cwd=cwd,
                env=child_env,
                **subprocess_config.kwargs,
            )
        except BaseException:
            _restore_parent_sigint()
            raise

        try:
            parent_death_link = (
                ParentDeathLink(parent_death_linked=True)
                if subprocess_config.parent_death_linked
                else link_child_lifetime_to_parent(
                    process.pid,
                    target_process_group=False,
                    scope_id="tui",
                    role="harness_tui",
                )
            )
        except BaseException:
            process.terminate()
            process.wait()
            _restore_parent_sigint()
            raise
        if not parent_death_link.parent_death_linked:
            process.terminate()
            process.wait()
            release_parent_death_link(parent_death_link)
            _restore_parent_sigint()
            raise RuntimeError("Native terminal child could not be linked to launcher lifetime")

        return _RunningNativeProcess(
            process=process,
            parent_death_link=parent_death_link,
            restore_parent_sigint=_restore_parent_sigint,
            parent_loop=parent_loop,
        )


__all__ = ["NativeProcessLauncher", "can_use_native_terminal"]
