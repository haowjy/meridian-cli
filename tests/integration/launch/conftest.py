"""Shared fixtures for launch integration tests.

Sets up a minimal project root (meridian.toml marker) at tmp_path and
points CWD there so that require_established_project_root() resolves
correctly for tests that depend on CWD-based root discovery.

Tests that need a different project root can override CWD via
monkeypatch.chdir() or pass explicit paths — this fixture only
establishes the baseline.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _launch_project_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write a meridian.toml marker and set CWD for launch tests.

    Scoped to root-discovery callers (e.g. test_streaming_serve). Tests that
    pass explicit project_root paths are unaffected by the CWD change.
    """
    (tmp_path / "meridian.toml").touch()
    monkeypatch.chdir(tmp_path)
