"""The history-blind bootstrap must trap runner-history reads in every Meridian launcher."""

import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import PACKAGE_ROOT

_ASSERT_TRAPPED = (
    "import sys\n"
    "from pathlib import Path\n"
    "path = Path(sys.argv[-1])\n"
    "try:\n"
    "    path.read_text()\n"
    "except AssertionError:\n"
    "    print('read trapped', file=sys.stderr)\n"
    "else:\n"
    "    raise SystemExit('runner history read was not trapped')\n"
)


def test_python_module_subprocess_traps_runner_history_reads(tmp_path: Path) -> None:
    blind_dir = PACKAGE_ROOT / "tests" / "support" / "runner_history_blind"
    history = tmp_path / "spawns" / "p1" / "history.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text("{}\n", encoding="utf-8")
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    (probe_dir / "read_probe.py").write_text(_ASSERT_TRAPPED, encoding="utf-8")
    # The CLI's own __main__ runs in this process; its reads are trapped too.
    indented = "".join(f"    {line}\n" for line in _ASSERT_TRAPPED.splitlines())
    (probe_dir / "cli_probe.py").write_text(
        "import runpy, sys\n"
        "argv = sys.argv\n"
        "sys.argv = ['meridian', '--version']\n"
        "try:\n"
        "    runpy.run_module('meridian', run_name='__main__', alter_sys=True)\n"
        "finally:\n"
        "    sys.argv = argv\n" + indented,
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        (str(blind_dir), str(probe_dir), str(PACKAGE_ROOT / "src"), env.get("PYTHONPATH", ""))
    )
    for module in ("read_probe", "cli_probe"):
        result = subprocess.run(
            [sys.executable, "-m", module, str(history)],
            env=env,
            cwd=PACKAGE_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "read trapped" in result.stderr
