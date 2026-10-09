"""CLI integration: tool-level commands run without a Meridian project."""

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


def _assert_rootless_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stderr or result.stdout
    assert NO_PROJECT_MSG not in (result.stdout + result.stderr)


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


_ROOTLESS_COMMANDS = (
    pytest.param(("qi",), None, id="qi"),
    pytest.param(("kg", "check"), None, id="kg-check"),
    pytest.param(("kg", "graph"), None, id="kg-graph"),
    pytest.param(("qi", "check"), None, id="qi-check"),
    pytest.param(("qi", "list"), "agents\tAGENTS.md", id="qi-list"),
    pytest.param(
        ("qi", "claude-md-fix", "--dry-run"),
        "[DRY-RUN] would create CLAUDE.md",
        id="qi-claude-md-fix-dry-run",
    ),
    pytest.param(("mermaid", "check"), None, id="mermaid-check"),
    pytest.param(("config", "show"), "project_root:", id="config-show"),
    pytest.param(
        ("config", "get", "defaults.max_depth"),
        "defaults.max_depth:",
        id="config-get-max-depth",
    ),
    pytest.param(("ext", "list"), "meridian.config", id="ext-list"),
    pytest.param(("ext", "commands"), "meridian.config.get:", id="ext-commands"),
)


@pytest.mark.integration
@pytest.mark.parametrize(("args", "expected_output"), _ROOTLESS_COMMANDS)
def test_rootless_tool_command_without_project(
    no_project_workspace: Path,
    meridian_home: Path,
    args: tuple[str, ...],
    expected_output: str | None,
) -> None:
    result = _run_meridian(list(args), cwd=no_project_workspace, meridian_home=meridian_home)
    _assert_rootless_success(result)
    if expected_output is not None:
        assert expected_output in result.stdout
