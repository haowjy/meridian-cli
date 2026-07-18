"""Unit tests for CLI established-project resolution."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meridian.cli.utils import resolve_cli_project_root

if TYPE_CHECKING:
    import pytest


def test_resolve_cli_project_root_establishes_cwd_with_project_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "meridian.toml").write_text(
        '[project]\nid = "proj-cwd"\n', encoding="utf-8"
    )
    monkeypatch.chdir(project_root)
    monkeypatch.delenv("MERIDIAN_PROJECT_DIR", raising=False)

    resolution = resolve_cli_project_root()

    assert resolution.established is True
    assert resolution.project_root == project_root.resolve()
    assert resolution.source == "cwd"


def test_resolve_cli_project_root_accepts_bare_cwd_without_project_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bare_cwd = tmp_path / "no-project"
    bare_cwd.mkdir()
    monkeypatch.chdir(bare_cwd)
    monkeypatch.delenv("MERIDIAN_PROJECT_DIR", raising=False)

    resolution = resolve_cli_project_root()

    assert resolution.established is True
    assert resolution.project_root == bare_cwd.resolve()
    assert resolution.source == "cwd"


def test_resolve_cli_project_root_env_still_establishes_without_cwd_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    other_cwd = tmp_path / "elsewhere"
    project_root.mkdir()
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())

    resolution = resolve_cli_project_root()

    assert resolution.established is True
    assert resolution.project_root == project_root.resolve()
    assert resolution.source == "env"
