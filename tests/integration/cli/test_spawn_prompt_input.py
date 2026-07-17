"""CLI integration coverage for explicit spawn prompt sources."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import posix_only


@pytest.mark.integration
@posix_only
def test_spawn_with_reference_does_not_read_silent_open_stdin(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / ".meridian").mkdir(parents=True)
    (project_root / ".meridian" / "id").write_text("prompt-input-test", encoding="utf-8")
    (project_root / "meridian.toml").write_text(
        "[spawn]\ndeny_headless_harnesses = []\n",
        encoding="utf-8",
    )
    reference = project_root / "reference.md"
    reference.write_text("Reference context.\n", encoding="utf-8")

    env = os.environ.copy()
    env["MERIDIAN_HOME"] = (tmp_path / "home").as_posix()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "meridian",
            "--harness",
            "codex",
            "spawn",
            "-a",
            "",
            "--bg",
            "--dry-run",
            "-f",
            reference.as_posix(),
        ],
        cwd=project_root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        return_code = process.wait(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdin is not None:
            process.stdin.close()

    assert return_code == 0
