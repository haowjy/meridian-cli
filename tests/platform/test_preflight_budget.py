"""The fast gate owns descendants even after its command leader exits."""

from __future__ import annotations

import os
import signal
import sys
import textwrap
from pathlib import Path

import psutil
import pytest

from meridian.dev.preflight import EXIT_BUDGET_EXHAUSTED, run_commands


@pytest.mark.parametrize("leader_exits", [False, True], ids=["timeout", "leader-exit"])
def test_fast_gate_cleans_term_resistant_descendants(
    tmp_path: Path, leader_exits: bool
) -> None:
    pid_file = tmp_path / "child.pid"
    # A native shell avoids cold interpreter startup in the readiness handshake.
    # Ignored TERM survives exec, so the sleeping child requires group SIGKILL.
    child = 'trap "" TERM; echo "$$" > "$1"; exec sleep 60'
    leader = textwrap.dedent(
        """
        import pathlib, subprocess, sys, time
        marker = pathlib.Path(sys.argv[1])
        child = subprocess.Popen(["sh", "-c", sys.argv[2], "child", str(marker)])
        while not marker.exists():
            time.sleep(0.01)
        if sys.argv[3] == "wait":
            child.wait()
        """
    )
    mode = "exit" if leader_exits else "wait"
    command = (sys.executable, "-c", leader, str(pid_file), child, mode)
    try:
        status = run_commands(
            [command],
            cwd=tmp_path,
            budget_seconds=1.0,
        )
        assert status == (0 if leader_exits else EXIT_BUDGET_EXHAUSTED)
        pid = int(pid_file.read_text())
        try:
            process = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        # An adopted zombie is dead even if the host's reaper has not collected it.
        assert not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
    finally:
        if pid_file.exists():
            pid = int(pid_file.read_text())
            try:
                if psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
                    os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, psutil.NoSuchProcess):
                pass
