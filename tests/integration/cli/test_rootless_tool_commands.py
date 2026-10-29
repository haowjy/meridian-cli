"""CLI integration: a tool-level command runs without a Meridian project."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

NO_PROJECT_MSG = "No Meridian project found"


def _run_meridian(
    args: list[str],
    *,
    cwd: Path,
    meridian_home: Path,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in tuple(env):
        if key.upper().startswith("MERIDIAN_"):
            env.pop(key, None)
    env["MERIDIAN_HOME"] = meridian_home.as_posix()
    return subprocess.run(
        [sys.executable, "-m", "meridian", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def no_project_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "readme.md").write_text("# hello\n", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
    return workspace


@pytest.fixture
def meridian_home(tmp_path: Path) -> Path:
    home = tmp_path / "meridian-home"
    home.mkdir()
    return home


@pytest.mark.integration
def test_rootless_qi_list_reports_discovery_without_project(
    no_project_workspace: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(["qi", "list"], cwd=no_project_workspace, meridian_home=meridian_home)

    assert result.returncode == 0, result.stderr or result.stdout
    assert NO_PROJECT_MSG not in (result.stdout + result.stderr)
    assert "agents\tAGENTS.md" in result.stdout
