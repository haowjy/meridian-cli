from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from meridian.lib.hooks.builtin.git_autosync import GitAutosync

if TYPE_CHECKING:
    import pytest


def _completed() -> subprocess.CompletedProcess[str]:
    result: subprocess.CompletedProcess[str] = MagicMock(spec=subprocess.CompletedProcess)
    result.stdout = ""
    result.stderr = ""
    result.returncode = 0
    return result


def test_run_git_strips_repo_scoped_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_DIR", "/tmp/real-checkout/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/tmp/real-checkout")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/tmp/test-gitconfig")

    with patch("subprocess.run", return_value=_completed()) as mock_run:
        GitAutosync()._run_git("/tmp/work", ["status"], timeout=5)

    call_env = mock_run.call_args.kwargs["env"]
    assert "GIT_DIR" not in call_env
    assert "GIT_WORK_TREE" not in call_env
    assert call_env["GIT_CONFIG_GLOBAL"] == "/tmp/test-gitconfig"
    assert call_env["LC_ALL"] == "C"
