"""Native terminal process launcher with launcher-owned lifecycle guarantees."""

from __future__ import annotations

import subprocess
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.detached_process import (
    ParentDeathLink,
    ParentWatchdog,
    process_scope_for_parent_watchdog,
    release_parent_death_link,
    start_parent_watchdog,
)

from .ports import PRIMARY_STDERR_LOG_PATH_ENV, LaunchedProcess, ProcessLauncher


@dataclass
class _RunningNativeProcess:
    process: subprocess.Popen[bytes]
    parent_death_link: ParentDeathLink
    wait_cancelled: threading.Event = field(default_factory=threading.Event)

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
        """Unblock ``wait``; scope teardown remains the owner's responsibility."""

        self.wait_cancelled.set()

    def wait(self) -> LaunchedProcess:
        try:
            while True:
                try:
                    exit_code = self.process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    if self.wait_cancelled.is_set():
                        return LaunchedProcess(exit_code=130, pid=self.process.pid)
                    continue
                return LaunchedProcess(exit_code=exit_code, pid=self.process.pid)
        finally:
            release_parent_death_link(self.parent_death_link)


class NativeProcessLauncher(ProcessLauncher):
    """Launch a TUI on inherited stdio without changing its process group."""

    def start(
        self,
        *,
        command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        output_log_path: Path | None,
        parent_watchdog: ParentWatchdog | None = None,
    ) -> _RunningNativeProcess:
        if IS_WINDOWS:
            raise RuntimeError("Native POSIX terminal launcher is not available on Windows")
        if output_log_path is not None:
            raise ValueError("Native terminal launcher does not capture output")

        owned_watchdog = None
        if parent_watchdog is None:
            owned_watchdog = start_parent_watchdog()
            parent_watchdog = owned_watchdog
        if parent_watchdog is None:
            raise RuntimeError("Native terminal parent watchdog could not be started")

        child_env = dict(env)
        child_env.pop(PRIMARY_STDERR_LOG_PATH_ENV, None)
        try:
            process: subprocess.Popen[bytes] = subprocess.Popen(
                command,
                cwd=cwd,
                env=child_env,
            )
        except BaseException:
            if owned_watchdog is not None:
                owned_watchdog.close()
            raise

        scope = process_scope_for_parent_watchdog(
            process.pid,
            target_process_group=False,
            scope_id="tui",
            role="harness_tui",
        )
        if not parent_watchdog.register_scope(scope):
            process.terminate()
            process.wait()
            if owned_watchdog is not None:
                owned_watchdog.close()
            raise RuntimeError("Native terminal child could not be linked to launcher lifetime")

        return _RunningNativeProcess(
            process=process,
            parent_death_link=ParentDeathLink(
                owned_watchdog=owned_watchdog,
                parent_death_linked=True,
            ),
        )


__all__ = ["NativeProcessLauncher"]
