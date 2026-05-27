"""Unit tests for session CLI argument mapping."""

from __future__ import annotations

from meridian.cli import session_cmd
from meridian.lib.core.util import FormatContext
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


def test_session_log_no_truncate_maps_to_truncate_false(monkeypatch) -> None:
    captured: list[SessionLogInput] = []

    def _fake_session_log_sync(payload: SessionLogInput) -> SessionLogInput:
        captured.append(payload)
        return payload

    monkeypatch.setattr(session_cmd, "session_log_sync", _fake_session_log_sync)
    session_cmd._session_log(lambda _output: None, ref="c1", no_truncate=True)

    assert captured[0].truncate is False


def test_session_log_global_maps_to_payload(monkeypatch) -> None:
    captured: list[SessionLogInput] = []

    def _fake_session_log_sync(payload: SessionLogInput) -> SessionLogInput:
        captured.append(payload)
        return payload

    monkeypatch.setattr(session_cmd, "session_log_sync", _fake_session_log_sync)
    session_cmd._session_log(
        lambda _output: None,
        ref="c1",
        global_scope=True,
        around_ordinal=10,
        context=2,
    )

    assert captured[0].global_scope is True


def test_session_log_raw_sets_verbose_format_context(monkeypatch) -> None:
    captured: list[SessionLogInput] = []
    captured_ctx: list[FormatContext | None] = []

    def _fake_session_log_sync(payload: SessionLogInput) -> SessionLogInput:
        captured.append(payload)
        return payload

    def _emit(_payload: object, *, format_ctx: FormatContext | None = None) -> None:
        captured_ctx.append(format_ctx)

    monkeypatch.setattr(session_cmd, "session_log_sync", _fake_session_log_sync)
    session_cmd._session_log(_emit, ref="c1", raw=True)

    assert captured[0].ref == "c1"
    assert captured_ctx[0] == FormatContext(verbosity=1)
