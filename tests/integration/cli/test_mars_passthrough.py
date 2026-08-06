"""Real CLI coverage for the Mars passthrough process boundary."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from meridian import __version__
from tests.conftest import posix_only


@pytest.mark.integration
@posix_only
def test_mars_passthrough_streams_and_propagates_exit_with_meridian_env(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_bin = runtime_root / "bin"
    runtime_bin.mkdir(parents=True)
    shutil.copy(Path(sys.prefix) / "pyvenv.cfg", runtime_root / "pyvenv.cfg")
    (runtime_root / "lib").symlink_to(Path(sys.prefix) / "lib")
    python = runtime_bin / "python"
    python.symlink_to(sys.executable)

    record_path = tmp_path / "mars-record.json"
    mars = runtime_bin / "mars"
    mars.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['MARS_RECORD'], 'w', encoding='utf-8') as record:\n"
        "    payload = {'argv': sys.argv[1:], "
        "'managed': os.environ.get('MERIDIAN_MANAGED'), "
        "'version': os.environ.get('MERIDIAN_VERSION')}\n"
        "    json.dump(payload, record)\n"
        "print('mars stdout', flush=True)\n"
        "print('mars stderr', file=sys.stderr, flush=True)\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    mars.chmod(0o755)

    env = os.environ.copy()
    env["MARS_RECORD"] = str(record_path)
    env["_MERIDIAN_DEPTH"] = "1"
    env.pop("MERIDIAN_MANAGED", None)
    env.pop("MERIDIAN_PROJECT_DIR", None)
    result = subprocess.run(
        [str(python), "-m", "meridian", "mars", "models", "list"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 7
    assert result.stdout == "mars stdout\n"
    assert result.stderr == "mars stderr\n"
    assert json.loads(record_path.read_text()) == {
        "argv": ["models", "list"],
        "managed": "1",
        "version": __version__,
    }
