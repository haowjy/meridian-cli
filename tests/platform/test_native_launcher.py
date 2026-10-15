"""Native terminal inheritance and parent-death lifecycle integration."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import psutil

from tests.conftest import posix_only


def _wait_for_file(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 0:
            return
        time.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {path}")


def _process_is_running(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


@posix_only
def test_native_launcher_inherits_terminal_state_and_kills_tui_with_parent(
    tmp_path: Path,
) -> None:
    child_script = tmp_path / "fake_tui.py"
    child_state = tmp_path / "child.json"
    parent_state = tmp_path / "parent.json"
    child_pid_path = tmp_path / "child.pid"
    child_script.write_text(
        """
import json
import os
import signal
import sys
import time
from pathlib import Path

def fd_identity(fd):
    stat = os.fstat(fd)
    return [stat.st_dev, stat.st_ino]

Path(sys.argv[1]).write_text(json.dumps({
    "fds": [fd_identity(fd) for fd in (0, 1, 2)],
    "pgid": os.getpgrp(),
    "sid": os.getsid(0),
    "sigint_default": signal.getsignal(signal.SIGINT) != signal.SIG_IGN,
}))
time.sleep(60)
""".strip(),
        encoding="utf-8",
    )

    parent_code = """
import json
import os
import signal
import sys
from pathlib import Path

from meridian.lib.launch.process.native_launcher import NativeProcessLauncher

def fd_identity(fd):
    stat = os.fstat(fd)
    return [stat.st_dev, stat.st_ino]

child_script, child_state, parent_state, child_pid_path, cwd = sys.argv[1:]
running = NativeProcessLauncher().start(
    command=(sys.executable, child_script, child_state),
    cwd=Path(cwd),
    env=dict(os.environ),
    output_log_path=None,
)
Path(child_pid_path).write_text(str(running.pid))
Path(parent_state).write_text(json.dumps({
    "fds": [fd_identity(fd) for fd in (0, 1, 2)],
    "pgid": os.getpgrp(),
    "sid": os.getsid(0),
    "sigint_ignored": signal.getsignal(signal.SIGINT) == signal.SIG_IGN,
}))
running.wait()
""".strip()

    stdin_path = tmp_path / "stdin"
    stdout_path = tmp_path / "stdout"
    stderr_path = tmp_path / "stderr"
    stdin_path.write_bytes(b"")
    parent: subprocess.Popen[bytes] | None = None
    child_pid = 0
    try:
        with (
            stdin_path.open("rb") as stdin_handle,
            stdout_path.open("wb") as stdout_handle,
            stderr_path.open("wb") as stderr_handle,
        ):
            parent = subprocess.Popen(
                (
                    sys.executable,
                    "-c",
                    parent_code,
                    str(child_script),
                    str(child_state),
                    str(parent_state),
                    str(child_pid_path),
                    str(tmp_path),
                ),
                stdin=stdin_handle,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )
            _wait_for_file(parent_state)
            _wait_for_file(child_state)
            _wait_for_file(child_pid_path)

            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            parent_facts = json.loads(parent_state.read_text(encoding="utf-8"))
            child_facts = json.loads(child_state.read_text(encoding="utf-8"))

            assert child_facts["fds"] == parent_facts["fds"]
            assert child_facts["pgid"] == parent_facts["pgid"]
            assert child_facts["sid"] == parent_facts["sid"]
            assert child_facts["sigint_default"] is True
            assert parent_facts["sigint_ignored"] is True

            os.kill(parent.pid, signal.SIGKILL)
            parent.wait(timeout=5.0)

        deadline = time.monotonic() + 5.0
        while _process_is_running(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _process_is_running(child_pid) is False
    finally:
        if parent is not None and parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5.0)
        if child_pid > 0 and _process_is_running(child_pid):
            with suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)
