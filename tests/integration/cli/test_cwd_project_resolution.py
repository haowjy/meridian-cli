"""CLI integration: cwd with .meridian/id establishes a project."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

NO_PROJECT_MSG = "No Meridian project found. Run from the project root or pass -C <path>."


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


def _assert_project_initialized(project_root: Path) -> None:
    assert (project_root / "meridian.toml").is_file()
    assert (project_root / ".meridian" / "id").is_file()


def _assert_project_not_initialized(project_root: Path) -> None:
    assert not (project_root / "meridian.toml").exists()
    assert not (project_root / ".meridian").exists()


@pytest.fixture
def meridian_home(tmp_path: Path) -> Path:
    home = tmp_path / "meridian-home"
    home.mkdir()
    return home


@pytest.fixture
def cwd_with_project_id(tmp_path: Path) -> Path:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".meridian").mkdir()
    (project_root / ".meridian" / "id").write_text("proj-cwd-integration", encoding="utf-8")
    return project_root


@pytest.fixture
def cwd_without_project_id(tmp_path: Path) -> Path:
    bare_cwd = tmp_path / "no-project"
    bare_cwd.mkdir()
    return bare_cwd


@pytest.mark.integration
def test_spawn_list_succeeds_from_cwd_with_project_id(
    cwd_with_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(["spawn", "list"], cwd=cwd_with_project_id, meridian_home=meridian_home)

    assert result.returncode == 0, result.stderr or result.stdout
    assert NO_PROJECT_MSG not in (result.stdout + result.stderr)


@pytest.mark.integration
def test_spawn_list_fails_from_cwd_without_project_id(
    cwd_without_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(
        ["spawn", "list"],
        cwd=cwd_without_project_id,
        meridian_home=meridian_home,
    )

    assert result.returncode == 1
    assert NO_PROJECT_MSG in (result.stdout + result.stderr)
    _assert_project_not_initialized(cwd_without_project_id)


@pytest.mark.integration
def test_init_succeeds_from_cwd_without_project_id(
    cwd_without_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(
        ["--mode", "human", "init"],
        cwd=cwd_without_project_id,
        meridian_home=meridian_home,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert NO_PROJECT_MSG not in (result.stdout + result.stderr)
    _assert_project_initialized(cwd_without_project_id)


@pytest.mark.integration
def test_init_succeeds_for_explicit_path_from_cwd_without_project_id(
    cwd_without_project_id: Path,
    meridian_home: Path,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target-project"

    result = _run_meridian(
        ["--mode", "human", "init", target.as_posix()],
        cwd=cwd_without_project_id,
        meridian_home=meridian_home,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert NO_PROJECT_MSG not in (result.stdout + result.stderr)
    _assert_project_initialized(target)
    _assert_project_not_initialized(cwd_without_project_id)


@pytest.mark.integration
def test_primary_launch_auto_initializes_cwd_without_project_id(
    cwd_without_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(
        [
            "--mode",
            "human",
            "--harness",
            "definitely-missing",
            "--timeout",
            "0.1",
        ],
        cwd=cwd_without_project_id,
        meridian_home=meridian_home,
    )

    assert result.returncode == 1
    assert NO_PROJECT_MSG not in (result.stdout + result.stderr)
    _assert_project_initialized(cwd_without_project_id)


@pytest.mark.integration
@pytest.mark.parametrize(
    "args",
    [
        ["--mode", "human", "config", "init"],
        ["--mode", "human", "config", "set", "defaults.max_depth", "7"],
        ["--mode", "human", "work", "start", "first work"],
        ["--mode", "human", "--harness", "definitely-missing", "spawn", "-p", "hi"],
        ["--mode", "human", "workspace", "init"],
    ],
)
def test_create_commands_auto_initialize_cwd_without_project_id(
    args: list[str],
    cwd_without_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(
        args,
        cwd=cwd_without_project_id,
        meridian_home=meridian_home,
    )

    assert NO_PROJECT_MSG not in (result.stdout + result.stderr)
    _assert_project_initialized(cwd_without_project_id)


@pytest.mark.integration
@pytest.mark.parametrize(
    "args",
    [
        ["--mode", "human", "spawn", "cancel", "p999"],
        ["--mode", "human", "config", "reset", "defaults.max_depth"],
        ["--mode", "human", "work", "list"],
        ["--mode", "human", "streaming", "test"],
        ["--mode", "human", "test", "harness"],
    ],
)
def test_strict_commands_do_not_auto_initialize_cwd_without_project_id(
    args: list[str],
    cwd_without_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(
        args,
        cwd=cwd_without_project_id,
        meridian_home=meridian_home,
    )

    assert result.returncode == 1
    assert NO_PROJECT_MSG in (result.stdout + result.stderr)
    _assert_project_not_initialized(cwd_without_project_id)


@pytest.mark.integration
def test_primary_launch_does_not_scaffold_config_for_established_cwd(
    cwd_with_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(
        [
            "--mode",
            "human",
            "--harness",
            "definitely-missing",
            "--timeout",
            "0.1",
        ],
        cwd=cwd_with_project_id,
        meridian_home=meridian_home,
    )

    assert result.returncode == 1
    assert NO_PROJECT_MSG not in (result.stdout + result.stderr)
    assert not (cwd_with_project_id / "meridian.toml").exists()


@pytest.mark.integration
def test_doctor_runs_from_cwd_with_project_id(
    cwd_with_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(["doctor"], cwd=cwd_with_project_id, meridian_home=meridian_home)

    assert result.returncode == 0, result.stderr or result.stdout
    assert NO_PROJECT_MSG not in (result.stdout + result.stderr)
    assert "project_root:" in result.stdout


@pytest.mark.integration
def test_doctor_runs_from_cwd_without_project_id(
    cwd_without_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(["doctor"], cwd=cwd_without_project_id, meridian_home=meridian_home)

    assert result.returncode == 0, result.stderr or result.stdout
    assert NO_PROJECT_MSG not in (result.stdout + result.stderr)
    assert "project_root:" in result.stdout
