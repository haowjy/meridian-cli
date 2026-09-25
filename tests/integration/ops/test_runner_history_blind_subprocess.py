"""The history-blind bootstrap must cover every Meridian launcher."""

import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import PACKAGE_ROOT

_ASSERT_PATCHED = (
    "from meridian.lib.state import history\n"
    "from meridian.lib.streaming import spawn_manager\n"
    "from meridian.lib.launch.process import primary_attach\n"
    "assert history.HarnessHistoryWriter.__name__ == 'DirectWriterNoop'\n"
    "assert spawn_manager.HarnessHistoryWriter.__name__ == 'AbsentWriter'\n"
    "assert primary_attach.HarnessHistoryWriter.__name__ == 'AbsentWriter'\n"
)


def test_python_module_subprocess_patches_writers_after_import(tmp_path: Path) -> None:
    blind_dir = PACKAGE_ROOT / "tests" / "support" / "runner_history_blind"
    env = os.environ.copy()
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    (probe_dir / "writer_probe.py").write_text(_ASSERT_PATCHED, encoding="utf-8")
    # The CLI's own __main__ runs in this process; the writers it can reach are patched.
    indented = "".join(f"    {line}\n" for line in _ASSERT_PATCHED.splitlines())
    (probe_dir / "cli_probe.py").write_text(
        "import runpy, sys\n"
        "sys.argv = ['meridian', '--version']\n"
        "try:\n"
        "    runpy.run_module('meridian', run_name='__main__', alter_sys=True)\n"
        "finally:\n" + indented + "    print('writers patched', file=sys.stderr)\n",
        encoding="utf-8",
    )
    env["PYTHONPATH"] = os.pathsep.join(
        (str(blind_dir), str(probe_dir), str(PACKAGE_ROOT / "src"), env.get("PYTHONPATH", ""))
    )
    for module in ("writer_probe", "cli_probe"):
        result = subprocess.run(
            [sys.executable, "-m", module],
            env=env,
            cwd=PACKAGE_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    assert "writers patched" in result.stderr
