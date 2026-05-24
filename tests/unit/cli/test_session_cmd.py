"""Unit tests for session CLI argument mapping."""

from __future__ import annotations

from meridian.cli import session_cmd
from meridian.lib.ops.session_log import SessionLogInput


def test_session_log_tail_bare_maps_to_default_five(monkeypatch) -> None:
    captured: list[SessionLogInput] = []

    def _fake_session_log_sync(payload: SessionLogInput) -> SessionLogInput:
        captured.append(payload)
        return payload

    monkeypatch.setattr(session_cmd, "session_log_sync", _fake_session_log_sync)
    emitted: list[object] = []
    session_cmd._session_log(lambda output: emitted.append(output), ref="c1", tail=[])

    assert captured[0].tail == 5
    assert emitted[0] == captured[0]


def test_session_log_tail_value_maps_directly(monkeypatch) -> None:
    captured: list[SessionLogInput] = []

    def _fake_session_log_sync(payload: SessionLogInput) -> SessionLogInput:
        captured.append(payload)
        return payload

    monkeypatch.setattr(session_cmd, "session_log_sync", _fake_session_log_sync)
    session_cmd._session_log(lambda _output: None, ref="c1", tail=[9])

    assert captured[0].tail == 9
