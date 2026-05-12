"""Shared fixtures for chat integration tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _chat_project_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set CWD to tmp_path and write a meridian.toml marker for every chat test.

    Chat tests frequently call run_chat_server() without an explicit entrypoint,
    which triggers require_established_project_root().  Without this fixture the
    test either relies on MERIDIAN_PROJECT_DIR (set locally, not in CI) or the
    repo root being the CWD.  Providing a consistent project-scoped tmp_path
    makes every test environment-independent.
    """
    (tmp_path / "meridian.toml").touch()
    monkeypatch.chdir(tmp_path)
