"""Shared fixtures for config integration tests.

Consolidated from per-file _isolate_config_env in test_project_config_ops.py.
test_settings_paths.py and test_workspace.py in the same directory have
similar isolation needs and benefit from this shared fixture too.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_config_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate config tests from inherited environment."""
    monkeypatch.delenv("MERIDIAN_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("MERIDIAN_PROJECT_DIR", raising=False)
    monkeypatch.delenv("MERIDIAN_CONFIG", raising=False)
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "user-home").as_posix())
