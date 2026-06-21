"""Cursor harness adapter capability tests."""

from __future__ import annotations

from meridian.lib.harness.cursor import CursorAdapter


def test_cursor_adapter_supports_session_resume() -> None:
    adapter = CursorAdapter()
    assert adapter.capabilities.supports_session_resume is True
