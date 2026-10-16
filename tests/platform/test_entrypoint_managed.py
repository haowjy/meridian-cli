"""CLI entrypoint managed-environment scoping."""

from __future__ import annotations

import os
import sys
import types

import pytest

import meridian.cli.entrypoint as entrypoint_module


def _make_fake_main_module(captured: dict[str, object]) -> types.ModuleType:
    fake = types.ModuleType("meridian.cli.main")

    def _noop(*, argv: list[str]) -> None:
        captured["argv"] = argv
        captured["managed"] = os.environ.get("MERIDIAN_MANAGED")

    fake.main = _noop  # type: ignore[attr-defined]
    return fake


@pytest.mark.parametrize(("outer", "expected"), [(None, "1"), ("0", "0")])
def test_entrypoint_scopes_meridian_managed(
    monkeypatch: pytest.MonkeyPatch,
    outer: str | None,
    expected: str,
) -> None:
    captured: dict[str, object] = {}
    fake_main_module = _make_fake_main_module(captured)
    original = sys.modules.get("meridian.cli.main")
    sys.modules["meridian.cli.main"] = fake_main_module

    try:
        if outer is None:
            monkeypatch.delenv("MERIDIAN_MANAGED", raising=False)
        else:
            monkeypatch.setenv("MERIDIAN_MANAGED", outer)
        monkeypatch.setattr(sys, "argv", ["meridian", "spawn", "list"])

        entrypoint_module.main()
    finally:
        if original is not None:
            sys.modules["meridian.cli.main"] = original
        else:
            sys.modules.pop("meridian.cli.main", None)

    assert captured["argv"] == ["spawn", "list"]
    assert captured["managed"] == expected
    if outer is None:
        assert "MERIDIAN_MANAGED" not in os.environ
    else:
        assert os.environ["MERIDIAN_MANAGED"] == outer
