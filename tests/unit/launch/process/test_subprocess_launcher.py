"""Subprocess launcher tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from meridian.lib.launch.process.ports import PRIMARY_STDERR_LOG_PATH_ENV
from meridian.lib.launch.process.subprocess_launcher import SubprocessProcessLauncher


def test_subprocess_launcher_tees_stderr_to_log_when_requested(
    monkeypatch,
    tmp_path: Path,
) -> None:
    stderr_path = tmp_path / "stderr.log"
    env = dict(os.environ)
    env[PRIMARY_STDERR_LOG_PATH_ENV] = str(stderr_path)

    monkeypatch.setattr(
        "meridian.lib.launch.process.subprocess_launcher._write_chunk_to_stderr",
        lambda _chunk: None,
    )

    outcome = SubprocessProcessLauncher().launch(
        command=(
            sys.executable,
            "-c",
            "import sys;sys.stderr.write('stderr-line\\n');sys.stderr.flush()",
        ),
        cwd=tmp_path,
        env=env,
        output_log_path=None,
    )

    assert outcome.exit_code == 0
    assert stderr_path.read_text(encoding="utf-8") == "stderr-line\n"
