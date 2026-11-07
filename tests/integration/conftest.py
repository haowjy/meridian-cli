"""Auto-mark integration tests collected under tests/integration/."""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        path = str(item.fspath).replace("\\", "/")
        if "/tests/integration/" in path and item.get_closest_marker("integration") is None:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True)
def short_ask_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the resident hold's 30 s ask grace out of tests that wait on it in real time."""
    monkeypatch.setattr("meridian.lib.streaming.resident_drain.ASK_GRACE_SECONDS", 0.01)
