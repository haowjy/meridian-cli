from __future__ import annotations

import os
import signal
import threading
from pathlib import Path

from meridian.lib.launch.process.ports import LaunchedProcess, TerminalFds
from meridian.lib.launch.process.runner import run_primary_process_with_capture
from meridian.lib.platform.launch_gate import ProcessIdentity


def test_signal_during_blackbox_wait_cancels_zero_exit(tmp_path: Path) -> None:
    wait_started = threading.Event()
    stop_requested = threading.Event()
    fallback_signal_seen = False

    class FakeRunningProcess:
        identity = ProcessIdentity(pid=1234, birth_epoch=0.0, pgid=1234)
        terminate_calls = 0

        def wait(self) -> LaunchedProcess:
            nonlocal fallback_signal_seen
            wait_started.set()
            while not stop_requested.wait(0.01) and not fallback_signal_seen:
                pass
            return LaunchedProcess(exit_code=0, pid=self.identity.pid)

        def terminate(self) -> None:
            self.terminate_calls += 1
            stop_requested.set()

        def cancel_wait(self) -> None:
            return None

    running = FakeRunningProcess()

    class FakeLauncher:
        def start(
            self,
            *,
            command: tuple[str, ...],
            cwd: Path,
            env: dict[str, str],
            output_log_path: Path | None,
            terminal_fds: TerminalFds,
        ) -> FakeRunningProcess:
            _ = command, cwd, env, output_log_path, terminal_fds
            return running

    def fallback_handler(_signum: int, _frame: object) -> None:
        nonlocal fallback_signal_seen
        fallback_signal_seen = True

    previous_handler = signal.signal(signal.SIGTERM, fallback_handler)
    sender = threading.Thread(
        target=lambda: (wait_started.wait(), os.kill(os.getpid(), signal.SIGTERM)),
        daemon=True,
    )
    sender.start()
    try:
        result = run_primary_process_with_capture(
            ("fake-harness",),
            tmp_path,
            {},
            None,
            FakeLauncher(),
            lambda _identity: None,
            tmp_path / "launcher.log",
        )
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        sender.join(timeout=1)

    assert result.cancelled is True
    assert result.exit_code == 130
    assert running.terminate_calls == 1
