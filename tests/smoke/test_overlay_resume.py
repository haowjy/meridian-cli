"""Paid, opt-in check that Claude can resume a Meridian-started session.

Uses the real Claude login and leaves a small session under a temporary project
in the native store, which is the point: the harness alone must find it.
Skipped when ``claude`` is not installed; outside the default gate.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow


def test_claude_resumes_meridian_session(tmp_path: Path) -> None:
    claude = shutil.which("claude")
    if claude is None:
        pytest.skip("claude binary not available")

    project = tmp_path / "project"
    project.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MERIDIAN_", "_MERIDIAN_", "MARS_", "CLAUDE", "GIT_", "XDG_"))
    }
    # The test suite isolates HOME; the real login lives in the real one.
    env["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
    meridian = str(Path(__file__).resolve().parents[2] / ".venv/bin/meridian")
    token = "word" + secrets.token_hex(12)
    start = subprocess.run(
        [
            meridian,
            "--json",
            "--harness",
            "claude",
            "spawn",
            "-a",
            "",
            "-m",
            "claude-haiku-5-5",
            "-p",
            f"Remember this word: {token}. Reply OK.",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    print("Meridian start:", start.stdout, start.stderr)
    assert start.returncode == 0
    spawn_id = json.loads(start.stdout)["spawn_id"]
    show = subprocess.run(
        [meridian, "--json", "spawn", "show", spawn_id, "--verbose"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert show.returncode == 0, show.stderr
    session_id = re.search(r"\(([0-9a-f-]{36})\)", json.loads(show.stdout)["boundary_summary"])
    assert session_id, show.stdout
    resumed = subprocess.run(
        [
            claude,
            "--resume",
            session_id.group(1),
            "--model",
            "claude-haiku-5-5",
            "--output-format",
            "json",
            "-p",
            "What word did I ask you to remember? Reply with just the word.",
        ],
        cwd=project,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    print("Claude resume:", resumed.stdout, resumed.stderr)
    assert resumed.returncode == 0
    assert token in json.loads(resumed.stdout)["result"]
