"""The history-blind bootstrap must cover every Meridian launcher."""

import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import PACKAGE_ROOT


def test_python_module_subprocess_patches_writers_after_import(tmp_path: Path) -> None:
    blind_dir = PACKAGE_ROOT / "tests" / "support" / "runner_history_blind"
    env = os.environ.copy()
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    probe = (
        "from meridian.lib.state import history; "
        "from meridian.lib.streaming import spawn_manager; "
        "from meridian.lib.launch.process import primary_attach; "
        "assert history.HarnessHistoryWriter.__name__ == 'DirectWriterNoop'; "
        "assert spawn_manager.HarnessHistoryWriter.__name__ == 'AbsentWriter'; "
        "assert primary_attach.HarnessHistoryWriter.__name__ == 'AbsentWriter'"
    )
    (probe_dir / "writer_probe.py").write_text(probe, encoding="utf-8")
    env["PYTHONPATH"] = os.pathsep.join(
        (str(blind_dir), str(probe_dir), str(PACKAGE_ROOT / "src"), env.get("PYTHONPATH", ""))
    )
    result = subprocess.run(
        [sys.executable, "-m", "writer_probe"],
        env=env,
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    result = subprocess.run(
        [sys.executable, "-m", "meridian", "--version"],
        env=env,
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
