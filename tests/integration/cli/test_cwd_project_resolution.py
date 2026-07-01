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
    assert (cwd_without_project_id / "meridian.toml").is_file()
    assert (cwd_without_project_id / ".meridian" / "id").is_file()


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
    assert (target / "meridian.toml").is_file()
    assert (target / ".meridian" / "id").is_file()
    assert not (cwd_without_project_id / "meridian.toml").exists()
    assert not (cwd_without_project_id / ".meridian").exists()


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
