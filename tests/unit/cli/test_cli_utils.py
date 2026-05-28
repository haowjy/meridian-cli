"""Unit tests for shared CLI helper contracts."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from meridian.cli import utils as cli_utils


def test_cli_project_root_posix_returns_posix_string(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    monkeypatch.setattr(cli_utils, "require_established_project_root", lambda: project_root)

    assert cli_utils.cli_project_root_posix() == project_root.as_posix()
