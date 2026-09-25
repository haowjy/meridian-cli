"""Thin pytest bridge to the permanently guarded, plugin-free acceptance process."""

import os
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest


@pytest.mark.integration
def test_continue_replay_acceptance_in_isolated_interpreter(tmp_path: Path) -> None:
    root = Path(__file__).parents[3]
    before = os.environ.copy()
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(root / "tests/acceptance/run_continue_replay.py"),
            "--source-root",
            str(root),
            "--dependency-root",
            sysconfig.get_path("purelib"),
        ],
        cwd=tmp_path,
        env={"HOME": str(tmp_path), "PATH": "/nonexistent"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert os.environ == before
    assert result.returncode == 0, result.stdout + result.stderr
    assert "isolated replay acceptance passed" in result.stdout
