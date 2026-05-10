"""Tests for Windows UTF-8 stdout/stderr reconfiguration in the CLI entrypoint."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

import meridian.cli.entrypoint as entrypoint_module


class _FakeTextIOWrapper:
    """Minimal stand-in for sys.stdout / sys.stderr with reconfigure support."""

    def __init__(self, encoding: str = "cp1252") -> None:
        self.encoding = encoding
        self._reconfigure_calls: list[dict[str, object]] = []

    def reconfigure(self, **kwargs: object) -> None:
        self._reconfigure_calls.append(kwargs)
        if "encoding" in kwargs:
            self.encoding = str(kwargs["encoding"])

    def write(self, s: str) -> int:
        return len(s)

    def flush(self) -> None:
        pass


def _make_fake_main_module(captured: dict[str, object]) -> types.ModuleType:
    fake = types.ModuleType("meridian.cli.main")

    def _noop(*, argv: list[str]) -> None:
        captured["argv"] = argv

    fake.main = _noop  # type: ignore[attr-defined]
    return fake


# ---------------------------------------------------------------------------
# Windows reconfiguration — active on win32
# ---------------------------------------------------------------------------


def test_reconfigure_called_for_stdout_and_stderr_on_win32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both streams are reconfigured to utf-8 on Windows."""
    fake_stdout = _FakeTextIOWrapper(encoding="cp1252")
    fake_stderr = _FakeTextIOWrapper(encoding="cp1252")

    captured: dict[str, object] = {}
    fake_main_module = _make_fake_main_module(captured)
    original = sys.modules.get("meridian.cli.main")
    sys.modules["meridian.cli.main"] = fake_main_module

    try:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        monkeypatch.setattr(sys, "stderr", fake_stderr)
        monkeypatch.setattr(sys, "argv", ["meridian", "spawn", "list"])

        entrypoint_module.main()
    finally:
        if original is not None:
            sys.modules["meridian.cli.main"] = original
        else:
            sys.modules.pop("meridian.cli.main", None)

    assert fake_stdout._reconfigure_calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert fake_stderr._reconfigure_calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_reconfigure_not_called_on_posix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streams are not touched on non-Windows platforms."""
    fake_stdout = _FakeTextIOWrapper(encoding="utf-8")
    fake_stderr = _FakeTextIOWrapper(encoding="utf-8")

    captured: dict[str, object] = {}
    fake_main_module = _make_fake_main_module(captured)
    original = sys.modules.get("meridian.cli.main")
    sys.modules["meridian.cli.main"] = fake_main_module

    try:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        monkeypatch.setattr(sys, "stderr", fake_stderr)
        monkeypatch.setattr(sys, "argv", ["meridian", "spawn", "list"])

        entrypoint_module.main()
    finally:
        if original is not None:
            sys.modules["meridian.cli.main"] = original
        else:
            sys.modules.pop("meridian.cli.main", None)

    assert fake_stdout._reconfigure_calls == []
    assert fake_stderr._reconfigure_calls == []


def test_reconfigure_skipped_when_stream_lacks_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streams without reconfigure() are left alone (e.g. redirected pipes)."""

    class _NoReconfigure:
        def write(self, s: str) -> int:
            return len(s)

        def flush(self) -> None:
            pass

    fake_stdout = _NoReconfigure()
    fake_stderr = _NoReconfigure()

    captured: dict[str, object] = {}
    fake_main_module = _make_fake_main_module(captured)
    original = sys.modules.get("meridian.cli.main")
    sys.modules["meridian.cli.main"] = fake_main_module

    try:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        monkeypatch.setattr(sys, "stderr", fake_stderr)
        monkeypatch.setattr(sys, "argv", ["meridian", "spawn", "list"])

        # Should not raise even though reconfigure is absent.
        entrypoint_module.main()
    finally:
        if original is not None:
            sys.modules["meridian.cli.main"] = original
        else:
            sys.modules.pop("meridian.cli.main", None)

    # No attribute error means the guard worked.
    assert not hasattr(fake_stdout, "_reconfigure_calls")
