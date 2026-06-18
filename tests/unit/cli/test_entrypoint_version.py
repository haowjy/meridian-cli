"""Root version fast-path parsing edge cases."""

from __future__ import annotations

import sys

import pytest

import meridian.cli.entrypoint as entrypoint_module


def test_version_fast_path_validates_mode_before_printing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["meridian", "--mode", "bogus", "--version"])

    with pytest.raises(SystemExit):
        entrypoint_module.main()

    captured = capsys.readouterr()
    assert not captured.out.startswith("meridian ")
