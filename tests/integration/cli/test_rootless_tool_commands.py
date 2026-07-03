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


@pytest.mark.integration
def test_qi_root_without_project(
    no_project_workspace: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(["qi"], cwd=no_project_workspace, meridian_home=meridian_home)
    _assert_rootless_success(result)


@pytest.mark.integration
def test_kg_check_without_project(
    no_project_workspace: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(["kg", "check"], cwd=no_project_workspace, meridian_home=meridian_home)
    _assert_rootless_success(result)


@pytest.mark.integration
def test_kg_graph_without_project(
    no_project_workspace: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(["kg", "graph"], cwd=no_project_workspace, meridian_home=meridian_home)
    _assert_rootless_success(result)


@pytest.mark.integration
def test_qi_check_without_project(
    no_project_workspace: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(["qi", "check"], cwd=no_project_workspace, meridian_home=meridian_home)
    _assert_rootless_success(result)


@pytest.mark.integration
def test_qi_list_without_project(
    no_project_workspace: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(["qi", "list"], cwd=no_project_workspace, meridian_home=meridian_home)
    _assert_rootless_success(result)
    assert "agents\tAGENTS.md" in result.stdout


@pytest.mark.integration
def test_qi_claude_md_fix_without_project(
    no_project_workspace: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(
        ["qi", "claude-md-fix", "--dry-run"],
        cwd=no_project_workspace,
        meridian_home=meridian_home,
    )
    _assert_rootless_success(result)
    assert "[DRY-RUN] would create CLAUDE.md" in result.stdout


@pytest.mark.integration
def test_mermaid_check_without_project(
    no_project_workspace: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(
        ["mermaid", "check"],
        cwd=no_project_workspace,
        meridian_home=meridian_home,
    )
    _assert_rootless_success(result)


@pytest.mark.integration
def test_config_show_without_project(
    no_project_workspace: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(
        ["config", "show"],
        cwd=no_project_workspace,
        meridian_home=meridian_home,
    )
    _assert_rootless_success(result)
    assert "project_root:" in result.stdout


@pytest.mark.integration
def test_config_get_without_project(
    no_project_workspace: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(
        ["config", "get", "defaults.max_depth"],
        cwd=no_project_workspace,
        meridian_home=meridian_home,
    )
    _assert_rootless_success(result)
    assert "defaults.max_depth:" in result.stdout


@pytest.mark.integration
def test_ext_list_without_project(
    no_project_workspace: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(["ext", "list"], cwd=no_project_workspace, meridian_home=meridian_home)
    _assert_rootless_success(result)
    assert "meridian.config" in result.stdout


@pytest.mark.integration
def test_ext_commands_without_project(
    no_project_workspace: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(
        ["ext", "commands"],
        cwd=no_project_workspace,
        meridian_home=meridian_home,
    )
    _assert_rootless_success(result)
    assert "meridian.config.get:" in result.stdout
