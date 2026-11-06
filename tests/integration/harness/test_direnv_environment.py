"""Exercise direnv's real reversal protocol with isolated, non-secret fixtures."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from meridian.lib.launch.env import undo_direnv


def test_undo_direnv_missing_executable_refuses_inheritance() -> None:
    with pytest.raises(ValueError, match="direnv is not on PATH"):
        undo_direnv({"PATH": "", "DIRENV_DIFF": "sensitive-diff"})


@pytest.fixture
def direnv_environments(tmp_path: Path) -> tuple[dict[str, str], dict[str, str]]:
    executable = shutil.which("direnv")
    if executable is None:
        pytest.skip("direnv is not installed")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".envrc").write_text(
        "export ANTHROPIC_API_KEY=fake-test-key\n"
        "export CHANGED=after\nunset REMOVED\n"
        "export PATH=/direnv-only:$PATH\n"
        "export MERIDIAN_IGNORE_DIRENV=true\n"
        "export PI_CODING_AGENT_DIR=/direnv-auth\n"
        "export PI_CODING_AGENT_SESSION_DIR=/direnv-sessions\n"
        "export HOME=/direnv-home\n"
        "export MERIDIAN_HOME=/direnv-meridian\n"
        "export OPENCODE_CONFIG_CONTENT='"
        '{"provider":{"anthropic":{"options":{"apiKey":"fake-test-key"}}}}'
        "'\n"
    )
    base = {
        "HOME": str(tmp_path),
        "PATH": os.pathsep.join((str(Path(executable).parent), os.defpath)),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "CHANGED": "before",
        "REMOVED": "original",
        "OPENCODE_CONFIG_CONTENT": '{"permission":{"bash":"ask"}}',
    }
    subprocess.run([executable, "allow", str(project)], env=base, check=True, capture_output=True)
    active = json.loads(
        subprocess.check_output(
            [executable, "exec", str(project), executable, "dump", "json"],
            env=base,
        )
    )
    return base, active


def test_real_direnv_restores_environment(
    direnv_environments: tuple[dict[str, str], dict[str, str]],
) -> None:
    base, active = direnv_environments
    assert active["ANTHROPIC_API_KEY"] == "fake-test-key"
    restored = undo_direnv(active)
    assert "ANTHROPIC_API_KEY" not in restored
    assert "DIRENV_DIFF" not in restored
    assert restored["CHANGED"] == "before"
    assert restored["REMOVED"] == "original"
    assert restored["PATH"] == base["PATH"]
    assert undo_direnv(restored) == restored
    with pytest.raises(ValueError, match="environment restoration failed") as failure:
        undo_direnv({**base, "DIRENV_DIFF": "sensitive-invalid-diff"})
    assert "sensitive" not in str(failure.value)






