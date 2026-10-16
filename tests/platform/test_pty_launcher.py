"""Real nested-PTY coverage for primary terminal teardown."""

from __future__ import annotations

import os
import select
import signal
import struct
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

from meridian.lib.launch.process.ports import TerminalFds
from meridian.lib.platform import IS_WINDOWS, fcntl, pty, termios

_ENABLED_MODES = b"\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h\x1b[?1049h\x1b[?25l"
_RESTORED_MODES = b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1049l\x1b[?25h"
_RESTORED_MODES_WITHOUT_ALT_SCREEN = (
    b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?25h"
)
_FAKE_TERMINAL_FDS = TerminalFds(stdin=10, stdout=11, stderr=2)


@pytest.mark.skipif(IS_WINDOWS, reason="PTY relay is POSIX-only")
def test_non_tty_stdout_does_not_receive_terminal_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.launch.process import pty_launcher

    monkeypatch.setattr(pty_launcher.sys, "stdin", SimpleNamespace(fileno=lambda: 10))
    monkeypatch.setattr(pty_launcher.sys, "stdout", SimpleNamespace(fileno=lambda: 11))
    monkeypatch.setattr(pty_launcher.os, "isatty", lambda _fd: False)
    monkeypatch.setattr(
        pty_launcher.select,
        "select",
        lambda *_args, **_kwargs: ([12], [], []),
    )
    monkeypatch.setattr(pty_launcher.os, "read", lambda _fd, _size: b"")
    writes: list[tuple[int, bytes]] = []
    monkeypatch.setattr(pty_launcher.os, "write", lambda fd, data: writes.append((fd, data)))
    monkeypatch.setattr(pty_launcher.os, "waitpid", lambda _pid, _options: (99, 0))

    exit_code = pty_launcher._copy_primary_pty_output(
        child_pid=99,
        master_fd=12,
        output_log_path=None,
        terminal_fds=_FAKE_TERMINAL_FDS,
    )

    assert exit_code == 0
    assert writes == []


@pytest.mark.skipif(IS_WINDOWS, reason="PTY relay is POSIX-only")
def test_closed_stdout_ends_pty_forwarding_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.launch.process import pty_launcher

    monkeypatch.setattr(pty_launcher.sys, "stdin", SimpleNamespace(fileno=lambda: 10))
    monkeypatch.setattr(pty_launcher.sys, "stdout", SimpleNamespace(fileno=lambda: 11))
    monkeypatch.setattr(pty_launcher.os, "isatty", lambda _fd: False)
    monkeypatch.setattr(
        pty_launcher.select,
        "select",
        lambda *_args, **_kwargs: ([12], [], []),
    )
    monkeypatch.setattr(pty_launcher.os, "read", lambda _fd, _size: b"late child output")

    def closed_stdout(_fd: int, _data: bytes) -> int:
        raise BrokenPipeError

    monkeypatch.setattr(pty_launcher.os, "write", closed_stdout)
    monkeypatch.setattr(pty_launcher.os, "waitpid", lambda _pid, _options: (99, 0))
    output_log = tmp_path / "primary-output.bin"

    exit_code = pty_launcher._copy_primary_pty_output(
        child_pid=99,
        master_fd=12,
        output_log_path=output_log,
        terminal_fds=_FAKE_TERMINAL_FDS,
    )

    assert exit_code == 0
    assert output_log.read_bytes() == b"late child output"


@pytest.mark.skipif(IS_WINDOWS, reason="PTY relay is POSIX-only")
def test_closed_pane_oserror_ends_pty_forwarding_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.launch.process import pty_launcher

    monkeypatch.setattr(pty_launcher.sys, "stdin", SimpleNamespace(fileno=lambda: 10))
    monkeypatch.setattr(pty_launcher.sys, "stdout", SimpleNamespace(fileno=lambda: 11))
    monkeypatch.setattr(pty_launcher.os, "isatty", lambda _fd: False)
    monkeypatch.setattr(
        pty_launcher.select,
        "select",
        lambda *_args, **_kwargs: ([12], [], []),
    )
    monkeypatch.setattr(pty_launcher.os, "read", lambda _fd, _size: b"late child output")

    def closed_pane(_fd: int, _data: bytes) -> int:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(pty_launcher.os, "write", closed_pane)
    monkeypatch.setattr(pty_launcher.os, "waitpid", lambda _pid, _options: (99, 0))
    output_log = tmp_path / "primary-output.bin"

    exit_code = pty_launcher._copy_primary_pty_output(
        child_pid=99,
        master_fd=12,
        output_log_path=output_log,
        terminal_fds=_FAKE_TERMINAL_FDS,
    )

    assert exit_code == 0
    assert output_log.read_bytes() == b"late child output"


@pytest.mark.skipif(IS_WINDOWS, reason="PTY relay is POSIX-only")
def test_closed_pane_oserror_on_master_write_ends_stdin_forwarding_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.launch.process import pty_launcher

    monkeypatch.setattr(pty_launcher.sys, "stdin", SimpleNamespace(fileno=lambda: 10))
    monkeypatch.setattr(pty_launcher.sys, "stdout", SimpleNamespace(fileno=lambda: 11))
    monkeypatch.setattr(pty_launcher.os, "isatty", lambda _fd: False)

    ready_sets = iter(([10], [12]))
    monkeypatch.setattr(
        pty_launcher.select,
        "select",
        lambda *_args, **_kwargs: (next(ready_sets), [], []),
    )

    def read(fd: int, _size: int) -> bytes:
        if fd == 10:
            return b"typed input"
        return b""

    def write(fd: int, _data: bytes) -> int:
        if fd == 12:
            raise OSError(5, "Input/output error")
        return len(_data)

    monkeypatch.setattr(pty_launcher.os, "read", read)
    monkeypatch.setattr(pty_launcher.os, "write", write)
    monkeypatch.setattr(pty_launcher.os, "waitpid", lambda _pid, _options: (99, 0))

    exit_code = pty_launcher._copy_primary_pty_output(
        child_pid=99,
        master_fd=12,
        output_log_path=None,
        terminal_fds=_FAKE_TERMINAL_FDS,
    )

    assert exit_code == 0


@pytest.mark.skipif(IS_WINDOWS, reason="PTY relay is POSIX-only")
def test_stdin_read_oserror_marks_stdin_closed_and_forwards_to_master_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.launch.process import pty_launcher

    monkeypatch.setattr(pty_launcher.sys, "stdin", SimpleNamespace(fileno=lambda: 10))
    monkeypatch.setattr(pty_launcher.sys, "stdout", SimpleNamespace(fileno=lambda: 11))
    monkeypatch.setattr(pty_launcher.os, "isatty", lambda _fd: False)

    ready_sets = iter(([10], [12], [12]))
    monkeypatch.setattr(
        pty_launcher.select,
        "select",
        lambda *_args, **_kwargs: (next(ready_sets), [], []),
    )

    master_reads = iter((b"child output", b""))

    def read(fd: int, _size: int) -> bytes:
        if fd == 10:
            raise OSError(5, "Input/output error")
        return next(master_reads)

    writes: list[tuple[int, bytes]] = []
    monkeypatch.setattr(pty_launcher.os, "read", read)
    monkeypatch.setattr(
        pty_launcher.os,
        "write",
        lambda fd, data: writes.append((fd, data)) or len(data),
    )
    monkeypatch.setattr(pty_launcher.os, "waitpid", lambda _pid, _options: (99, 0))

    exit_code = pty_launcher._copy_primary_pty_output(
        child_pid=99,
        master_fd=12,
        output_log_path=None,
        terminal_fds=_FAKE_TERMINAL_FDS,
    )

    assert exit_code == 0
    assert writes == [(11, b"child output")]


@pytest.mark.skipif(IS_WINDOWS, reason="PTY relay is POSIX-only")
def test_child_alt_screen_exit_is_not_repeated_after_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.launch.process import pty_launcher

    child_output = (
        b"\x1b[?1049hframe\x1b[?1049l"
        b"Resume this session with: claude --resume session-id\r\n"
    )
    master_reads = iter((child_output, b""))
    writes: list[tuple[int, bytes]] = []

    monkeypatch.setattr(pty_launcher.sys, "stdin", SimpleNamespace(fileno=lambda: 10))
    monkeypatch.setattr(pty_launcher.sys, "stdout", SimpleNamespace(fileno=lambda: 11))
    monkeypatch.setattr(pty_launcher.os, "isatty", lambda fd: fd == 11)
    monkeypatch.setattr(
        pty_launcher.select,
        "select",
        lambda *_args, **_kwargs: ([12], [], []),
    )
    monkeypatch.setattr(pty_launcher.os, "read", lambda _fd, _size: next(master_reads))
    monkeypatch.setattr(
        pty_launcher.os,
        "write",
        lambda fd, data: writes.append((fd, data)) or len(data),
    )
    monkeypatch.setattr(pty_launcher.os, "waitpid", lambda _pid, _options: (99, 0))

    exit_code = pty_launcher._copy_primary_pty_output(
        child_pid=99,
        master_fd=12,
        output_log_path=None,
        terminal_fds=_FAKE_TERMINAL_FDS,
    )

    assert exit_code == 0
    assert writes == [
        (11, child_output),
        (11, _RESTORED_MODES_WITHOUT_ALT_SCREEN),
    ]


@pytest.mark.skipif(IS_WINDOWS, reason="PTY relay is POSIX-only")
def test_split_alt_screen_enter_is_restored_after_child_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.launch.process import pty_launcher

    master_reads = iter((b"\x1b[?10", b"49hframe", b""))
    writes: list[tuple[int, bytes]] = []

    monkeypatch.setattr(pty_launcher.sys, "stdin", SimpleNamespace(fileno=lambda: 10))
    monkeypatch.setattr(pty_launcher.sys, "stdout", SimpleNamespace(fileno=lambda: 11))
    monkeypatch.setattr(pty_launcher.os, "isatty", lambda fd: fd == 11)
    monkeypatch.setattr(
        pty_launcher.select,
        "select",
        lambda *_args, **_kwargs: ([12], [], []),
    )
    monkeypatch.setattr(pty_launcher.os, "read", lambda _fd, _size: next(master_reads))
    monkeypatch.setattr(
        pty_launcher.os,
        "write",
        lambda fd, data: writes.append((fd, data)) or len(data),
    )
    monkeypatch.setattr(pty_launcher.os, "waitpid", lambda _pid, _options: (99, 0))

    exit_code = pty_launcher._copy_primary_pty_output(
        child_pid=99,
        master_fd=12,
        output_log_path=None,
        terminal_fds=_FAKE_TERMINAL_FDS,
    )

    assert exit_code == 0
    assert writes == [
        (11, b"\x1b[?10"),
        (11, b"49hframe"),
        (11, _RESTORED_MODES),
    ]


@pytest.mark.skipif(IS_WINDOWS, reason="PTY relay is POSIX-only")
def test_wait_cancelled_after_eof_returns_130_without_waitpid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.launch.process import pty_launcher

    monkeypatch.setattr(pty_launcher.sys, "stdin", SimpleNamespace(fileno=lambda: 10))
    monkeypatch.setattr(pty_launcher.sys, "stdout", SimpleNamespace(fileno=lambda: 11))
    monkeypatch.setattr(pty_launcher.os, "isatty", lambda _fd: False)
    monkeypatch.setattr(pty_launcher.os, "read", lambda _fd, _size: b"")
    cancelled = threading.Event()

    def cancel_and_ready(
        *_args: object, **_kwargs: object
    ) -> tuple[list[int], list[int], list[int]]:
        cancelled.set()
        return [12], [], []

    monkeypatch.setattr(pty_launcher.select, "select", cancel_and_ready)
    monkeypatch.setattr(
        pty_launcher.os,
        "waitpid",
        lambda *_args, **_kwargs: pytest.fail("waitpid must not block a cancelled relay"),
    )

    exit_code = pty_launcher._copy_primary_pty_output(
        child_pid=99,
        master_fd=12,
        output_log_path=None,
        terminal_fds=_FAKE_TERMINAL_FDS,
        wait_cancelled=cancelled,
    )

    assert exit_code == 130


@pytest.mark.skipif(IS_WINDOWS, reason="PTY relay is POSIX-only")
def test_worker_thread_relay_forwards_terminal_resizes(tmp_path: Path) -> None:
    """Managed attach relays in a worker thread, where signal handlers cannot run."""

    outer_master, outer_slave = pty.openpty()
    fcntl.ioctl(outer_master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    relay_pid = os.fork()

    if relay_pid == 0:
        try:
            os.close(outer_master)
            os.dup2(outer_slave, 0)
            os.dup2(outer_slave, 1)
            if outer_slave > 1:
                os.close(outer_slave)
            sys.stdin = os.fdopen(os.dup(0), "r")
            sys.stdout = os.fdopen(os.dup(1), "w")

            from meridian.lib.launch.process.pty_launcher import PtyProcessLauncher

            child_code = """
import os
import signal
import time

resized = False

def handle_resize(_signum, _frame):
    global resized
    resized = True

def report(label):
    size = os.get_terminal_size()
    print(f"{label} {size.columns}x{size.lines}", flush=True)

signal.signal(signal.SIGWINCH, handle_resize)
report("INITIAL")
deadline = time.monotonic() + 2.0
while not resized and time.monotonic() < deadline:
    time.sleep(0.02)
if resized:
    report("RESIZED")
"""
            process = PtyProcessLauncher().start(
                command=(sys.executable, "-c", child_code),
                cwd=tmp_path,
                env=dict(os.environ),
                output_log_path=None,
                terminal_fds=TerminalFds(stdin=0, stdout=1, stderr=2),
            )
            waiter = threading.Thread(target=process.wait, daemon=True)
            waiter.start()
            waiter.join(timeout=5.0)
            if waiter.is_alive():
                process.terminate()
                process.cancel_wait()
                waiter.join(timeout=1.0)
                os._exit(2)
            os._exit(0)
        except BaseException:
            os._exit(1)

    os.close(outer_slave)
    terminal_output = bytearray()
    resized_outer = False
    relay_status: int | None = None
    deadline = time.monotonic() + 7.0
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([outer_master], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(outer_master, 4096)
                except OSError:
                    chunk = b""
                terminal_output.extend(chunk)
            if not resized_outer and b"INITIAL 80x24" in terminal_output:
                fcntl.ioctl(
                    outer_master,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", 30, 100, 0, 0),
                )
                resized_outer = True
            waited_pid, relay_status = os.waitpid(relay_pid, os.WNOHANG)
            if waited_pid == relay_pid:
                break
        else:
            os.kill(relay_pid, signal.SIGKILL)
            pytest.fail("worker-thread PTY relay did not exit")
    finally:
        with suppress(ChildProcessError):
            os.waitpid(relay_pid, 0)
        os.close(outer_master)

    assert relay_status is not None
    assert os.waitstatus_to_exitcode(relay_status) == 0
    assert resized_outer
    assert b"RESIZED 100x30" in terminal_output


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
                terminal_fds=TerminalFds(stdin=0, stdout=1, stderr=2),
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
