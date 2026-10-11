"""The fast gate treats its budget as a warning and always owns descendants."""

from __future__ import annotations

import os
import re
import signal
import sys
import textwrap
import time
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

from meridian.dev.preflight import run_commands

if TYPE_CHECKING:
    import pytest


def _assert_process_stopped(pid: int) -> None:
    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    # Signal delivery is asynchronous; observe death, not an arbitrary tick.
    # An adopted zombie is dead even if the host's reaper has not collected it.
    deadline = time.monotonic() + 1.0
    try:
        while process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
            assert time.monotonic() < deadline, "owned descendant survived cleanup"
            time.sleep(0.01)
    except psutil.NoSuchProcess:
        pass


def test_fast_gate_cleans_term_resistant_descendants_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_killpg = os.killpg
    leader_statuses: list[str] = []

    def record_group_cleanup(pgid: int, sig: int) -> None:
        leader_statuses.append(psutil.Process(pgid).status())
        real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", record_group_cleanup)
    child = 'trap "" TERM; echo "$$" > "$1"; exec sleep 60'
    leader = textwrap.dedent(
        """
        import pathlib, subprocess, sys, time
        marker = pathlib.Path(sys.argv[1])
        peer_marker = pathlib.Path(sys.argv[2])
        child = subprocess.Popen(["sh", "-c", sys.argv[3], "child", str(marker)])
        while not marker.exists() or not peer_marker.exists():
            time.sleep(0.01)
        if sys.argv[4] == "fail":
            sys.exit(17)
        child.wait()
        """
    )
    waiting_pid_file = tmp_path / "waiting-child.pid"
    failing_pid_file = tmp_path / "failing-child.pid"
    waiting = (
        sys.executable,
        "-c",
        leader,
        str(waiting_pid_file),
        str(failing_pid_file),
        child,
        "wait",
    )
    failing = (
        sys.executable,
        "-c",
        leader,
        str(failing_pid_file),
        str(waiting_pid_file),
        child,
        "fail",
    )
    try:
        assert run_commands([waiting, failing], cwd=tmp_path) == 17
        assert len(leader_statuses) == 2
        assert psutil.STATUS_ZOMBIE in leader_statuses
        _assert_process_stopped(int(waiting_pid_file.read_text()))
        _assert_process_stopped(int(failing_pid_file.read_text()))
    finally:
        for pid_file in (waiting_pid_file, failing_pid_file):
            if not pid_file.exists():
                continue
            try:
                process = psutil.Process(int(pid_file.read_text()))
                if process.status() != psutil.STATUS_ZOMBIE:
                    os.kill(process.pid, signal.SIGKILL)
            except (ProcessLookupError, psutil.NoSuchProcess):
                pass


def test_fast_gate_completes_all_checks_and_warns_over_budget(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    markers = [tmp_path / f"check-{index}" for index in range(3)]
    commands = [
        ("sh", "-c", 'sleep 0.05; printf done > "$1"', "check", str(marker))
        for marker in markers
    ]

    assert run_commands(commands, cwd=tmp_path, budget_seconds=0.001) == 0

    assert [marker.read_text() for marker in markers] == ["done", "done", "done"]
    output = capsys.readouterr().err.splitlines()
    assert re.fullmatch(r"preflight: fast gate passed in \d+\.\d{2}s", output[-2])
    assert re.fullmatch(
        r"preflight: warning: fast gate took \d+\.\ds \(target 0\.001s\)",
        output[-1],
    )


def test_fast_gate_uses_github_warning_annotation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    command = ("sh", "-c", "sleep 0.05")

    assert run_commands([command], cwd=tmp_path, budget_seconds=0.001) == 0

    output = capsys.readouterr().err.splitlines()
    assert re.fullmatch(
        r"::warning title=Fast gate over budget::fast gate took \d+\.\ds "
        r"\(target 0\.001s\)",
        output[-1],
    )
