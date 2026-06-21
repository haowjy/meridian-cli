"""Shared fixtures for launch integration tests.

Sets CWD and MERIDIAN_PROJECT_DIR to tmp_path so
require_established_project_root() resolves for tests that depend on
project targeting without passing -C explicitly.

Tests that need a different project root can override CWD or env via
monkeypatch — this fixture only establishes the baseline.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _launch_project_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin CWD and inherited project dir for launch integration tests."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", tmp_path.as_posix())
