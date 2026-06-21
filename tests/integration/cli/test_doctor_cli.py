"""CLI integration tests for meridian doctor startup policy."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_doctor_runs_without_established_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression: clean env + bare cwd must not hit require_established_project_root."""
    bare_cwd = tmp_path / "no-project"
    bare_cwd.mkdir()
    user_home = tmp_path / "user-home"
    user_home.mkdir()

    for key in list(os.environ):
        if key.upper().startswith("MERIDIAN"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    monkeypatch.chdir(bare_cwd)

    from meridian.cli.main import main

    with pytest.raises(SystemExit) as exc_info:
        main(["doctor"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "project_root:" in captured.out
    assert "No Meridian project found" not in captured.err
