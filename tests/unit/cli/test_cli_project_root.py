"""Unit tests for CLI established-project resolution."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meridian.cli.utils import resolve_cli_project_root
from meridian.lib.config.project_root import (
    cwd_has_project_id,
    resolve_project_root_resolution,
)

if TYPE_CHECKING:
    import pytest


def test_cwd_has_project_id_true_when_id_file_present(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".meridian").mkdir()
    (project_root / ".meridian" / "id").write_text("proj-test", encoding="utf-8")

    assert cwd_has_project_id(project_root) is True


def test_cwd_has_project_id_false_without_meridian_dir(tmp_path: Path) -> None:
    project_root = tmp_path / "bare"
    project_root.mkdir()

    assert cwd_has_project_id(project_root) is False


def test_cwd_has_project_id_false_for_ancestor_id_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    nested = project_root / "src"
    project_root.mkdir()
    nested.mkdir()
    (project_root / ".meridian").mkdir()
    (project_root / ".meridian" / "id").write_text("proj-parent", encoding="utf-8")
    monkeypatch.chdir(nested)

    resolution = resolve_project_root_resolution()
    assert resolution.source == "cwd"
    assert cwd_has_project_id(resolution.project_root) is False


def test_resolve_cli_project_root_establishes_cwd_with_project_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".meridian").mkdir()
    (project_root / ".meridian" / "id").write_text("proj-cwd", encoding="utf-8")
    monkeypatch.chdir(project_root)
    monkeypatch.delenv("MERIDIAN_PROJECT_DIR", raising=False)

    resolution = resolve_cli_project_root()

    assert resolution.established is True
    assert resolution.project_root == project_root.resolve()
    assert resolution.source == "cwd"


def test_resolve_cli_project_root_rejects_bare_cwd_without_project_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bare_cwd = tmp_path / "no-project"
    bare_cwd.mkdir()
    monkeypatch.chdir(bare_cwd)
    monkeypatch.delenv("MERIDIAN_PROJECT_DIR", raising=False)

    resolution = resolve_cli_project_root()

    assert resolution.established is False
    assert resolution.project_root is None
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
