from __future__ import annotations

import ast
import os
import pty
import select
import signal
import sys
import termios
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import pytest

from meridian.cli.browse.tui import run_browse_picker
from meridian.cli.session_cmd import _exec_decision
from meridian.lib.ops.session_list import SessionListOutput, SessionListRow
from meridian.lib.ops.session_reentry import Resume
from tests.conftest import posix_only


@posix_only
def test_picker_restores_terminal_before_exec_successor(tmp_path: Path) -> None:
    master_fd, slave_fd = pty.openpty()
    original_termios = termios.tcgetattr(slave_fd)
    child_pid = os.fork()

    if child_pid == 0:
        try:
            os.close(master_fd)
            os.login_tty(slave_fd)
            sys.stdin = os.fdopen(os.dup(0), "r")
            sys.stdout = os.fdopen(os.dup(1), "w")
            sys.stderr = os.fdopen(os.dup(2), "w")
            now = datetime.now(UTC).isoformat()
            listing = SessionListOutput(
                rows=(
                    SessionListRow(
                        chat_id="c1",
                        activity_at=now,
                        live=False,
                        reentry=Resume("c1"),
                        agent="coder",
                        model="gpt",
                        work_label="handoff-probe",
                        task_cwd=tmp_path.as_posix(),
                    ),
                ),
                total_count=1,
            )
            decision = run_browse_picker(
                listing,
                tmp_path.as_posix(),
                lambda chat_id: Resume(chat_id),
            )
            assert isinstance(decision, Resume)
            assert decision.chat_id == "c1"

            def exec_successor(_executable: str, _argv: list[str]) -> None:
                code = (
                    "import termios; "
                    "print('SUCCESSOR=' + repr(termios.tcgetattr(0)), flush=True)"
                )
                os.execv(sys.executable, [sys.executable, "-c", code])

            _exec_decision(
                decision,
                project_root=tmp_path.as_posix(),
                exec_fn=exec_successor,
            )
        except BaseException:
            import traceback

            traceback.print_exc()
            os._exit(1)

    os.close(slave_fd)
    output = bytearray()
    sent_enter = False
    child_status: int | None = None
    deadline = time.monotonic() + 5.0
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master_fd], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""
                output.extend(chunk)
                if not sent_enter and b"c1" in output:
                    os.write(master_fd, b"\r")
                    sent_enter = True
            waited_pid, status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                child_status = status
                break
        else:
            os.kill(child_pid, signal.SIGKILL)
            pytest.fail("session browse PTY child did not exit")
    finally:
        with suppress(ChildProcessError):
            os.waitpid(child_pid, 0)
        os.close(master_fd)

    assert sent_enter
    assert child_status is not None
    assert os.waitstatus_to_exitcode(child_status) == 0, output.decode(errors="replace")
    marker = b"SUCCESSOR="
    marker_index = output.index(marker)
    restored_index = output.rfind(b"\x1b[?1049l", 0, marker_index)
    assert restored_index >= 0
    successor_line = output[marker_index + len(marker) :].splitlines()[0].decode()
    assert ast.literal_eval(successor_line) == original_termios
