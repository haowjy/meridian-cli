"""Cursor harness adapter capability tests."""

from __future__ import annotations

from meridian.lib.harness.cursor import CursorAdapter


def test_cursor_adapter_declares_resume_support_for_continue_session_guard() -> None:
    adapter = CursorAdapter()

    # Continue resolution preserves requested Cursor session ids only when this
    # capability is true; keep this guard tied to that behavior switch.
    assert adapter.capabilities.supports_session_resume is True
