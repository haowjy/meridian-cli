"""Shared fixtures for ops integration tests.

Consolidated from per-file _isolate_runtime_env fixtures in:
  - test_diag.py
  - test_spawn_api.py
  - test_spawn_model_selection.py

MERIDIAN_HOME is included here (not just in the design-doc baseline) to
preserve the isolation that test_diag.py requires — config_show and
doctor_sync walk the user home and must not touch the real ~/.meridian.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_ops_runtime_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate ops tests from inherited runtime environment."""
    monkeypatch.delenv("MERIDIAN_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "user-home").as_posix())
