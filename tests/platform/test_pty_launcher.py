"""Real nested-PTY coverage for primary terminal teardown."""

from __future__ import annotations

import os
import select
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

from meridian.lib.platform import IS_WINDOWS, pty

_ENABLED_MODES = b"\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h\x1b[?1049h\x1b[?25l"
_RESTORED_MODES = b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1049l\x1b[?25h"


@pytest.mark.skipif(IS_WINDOWS, reason="PTY relay is POSIX-only")
def test_signalled_primary_child_restores_dec_modes_outside_capture(tmp_path: Path) -> None:
    """A killed TUI cannot leave its caller's terminal private modes enabled."""

    outer_master, outer_slave = pty.openpty()
    status_read, status_write = os.pipe()
    output_log = tmp_path / "primary-output.bin"
    relay_pid = os.fork()

    if relay_pid == 0:
        try:
            os.close(outer_master)
            os.close(status_read)
            os.dup2(outer_slave, 0)
            os.dup2(outer_slave, 1)
            if outer_slave > 1:
                os.close(outer_slave)
            sys.stdin = os.fdopen(os.dup(0), "r")
            sys.stdout = os.fdopen(os.dup(1), "w")

            from meridian.lib.launch.process.pty_launcher import PtyProcessLauncher

            process = PtyProcessLauncher().start(
                command=(
                    "/bin/sh",
                    "-c",
                    "printf '\\033[?1000h\\033[?1002h\\033[?1003h"
                    "\\033[?1006h\\033[?1049h\\033[?25l'; kill -TERM $$",
                ),
                cwd=tmp_path,
                env=dict(os.environ),
                output_log_path=output_log,
            )
            result = process.wait()
            os.write(status_write, str(result.exit_code).encode())
        except BaseException as exc:
            with suppress(OSError):
                os.write(status_write, f"error:{exc!r}".encode())
        finally:
            os._exit(0)

    os.close(outer_slave)
    os.close(status_write)
    terminal_output = bytearray()
    deadline = time.monotonic() + 5.0
    relay_status: int | None = None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([outer_master], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(outer_master, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    terminal_output.extend(chunk)
            waited_pid, relay_status = os.waitpid(relay_pid, os.WNOHANG)
            if waited_pid == relay_pid:
                break
        else:
            os.kill(relay_pid, signal.SIGKILL)
            pytest.fail("nested PTY relay did not exit")
    finally:
        with suppress(ChildProcessError):
            os.waitpid(relay_pid, 0)
        os.close(outer_master)

    child_exit = os.read(status_read, 32)
    os.close(status_read)

    assert relay_status is not None
    assert os.waitstatus_to_exitcode(relay_status) == 0
    assert child_exit == b"-15"
    assert _ENABLED_MODES in terminal_output
    assert terminal_output.endswith(_RESTORED_MODES)
    captured_child_output = output_log.read_bytes()
    assert _ENABLED_MODES in captured_child_output
    assert _RESTORED_MODES not in captured_child_output
