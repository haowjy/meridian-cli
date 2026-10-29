"""Tests for Windows UTF-8 stdout/stderr reconfiguration in the CLI entrypoint."""

from __future__ import annotations

import io
import os
import sys
import types

import pytest

import meridian.cli.entrypoint as entrypoint_module


class _FakeTextIOWrapper(io.TextIOWrapper):
    """Minimal stand-in for sys.stdout / sys.stderr with reconfigure support."""

    def __init__(self, encoding: str = "cp1252") -> None:
        super().__init__(io.BytesIO(), encoding=encoding)
        self._reconfigure_calls: list[dict[str, object]] = []

    def reconfigure(self, **kwargs: object) -> None:
        self._reconfigure_calls.append(kwargs)


def _make_fake_main_module(captured: dict[str, object]) -> types.ModuleType:
    fake = types.ModuleType("meridian.cli.main")

    def _noop(*, argv: list[str]) -> None:
        captured["argv"] = argv
        captured["managed"] = os.environ.get("MERIDIAN_MANAGED")

    fake.main = _noop  # type: ignore[attr-defined]
    return fake


def _run_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
    *,
    is_windows: bool,
    stdout: object,
    stderr: object,
) -> dict[str, object]:
    captured: dict[str, object] = {}
    original = sys.modules.get("meridian.cli.main")
    sys.modules["meridian.cli.main"] = _make_fake_main_module(captured)
    try:
        monkeypatch.setattr(entrypoint_module, "IS_WINDOWS", is_windows)
        monkeypatch.setattr(sys, "stdout", stdout)
        monkeypatch.setattr(sys, "stderr", stderr)
        monkeypatch.setattr(sys, "argv", ["meridian", "spawn", "list"])
        entrypoint_module.main()
    finally:
        if original is not None:
            sys.modules["meridian.cli.main"] = original
        else:
            sys.modules.pop("meridian.cli.main", None)
    return captured


# ---------------------------------------------------------------------------
# Windows reconfiguration — active on win32
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("is_windows", "expected_calls"),
    (
        pytest.param(True, [{"encoding": "utf-8", "errors": "replace"}], id="win32"),
        pytest.param(False, [], id="posix"),
    ),
)
def test_reconfigure_follows_platform(
    monkeypatch: pytest.MonkeyPatch,
    is_windows: bool,
    expected_calls: list[dict[str, object]],
) -> None:
    """Both streams are reconfigured to utf-8 on Windows and left alone elsewhere."""
    fake_stdout = _FakeTextIOWrapper(encoding="cp1252")
    fake_stderr = _FakeTextIOWrapper(encoding="cp1252")

    _run_entrypoint(
        monkeypatch,
        is_windows=is_windows,
        stdout=fake_stdout,
        stderr=fake_stderr,
    )

    assert fake_stdout._reconfigure_calls == expected_calls
    assert fake_stderr._reconfigure_calls == expected_calls


def test_entrypoint_defaults_meridian_managed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MERIDIAN_MANAGED", raising=False)

    captured = _run_entrypoint(
        monkeypatch,
        is_windows=False,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )

    assert captured["argv"] == ["spawn", "list"]
    assert captured["managed"] == "1"
    assert "MERIDIAN_MANAGED" not in os.environ


def test_entrypoint_preserves_outer_meridian_managed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_MANAGED", "0")

    captured = _run_entrypoint(
        monkeypatch,
        is_windows=False,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )

    assert captured["argv"] == ["spawn", "list"]
    assert captured["managed"] == "0"
    assert os.environ["MERIDIAN_MANAGED"] == "0"
