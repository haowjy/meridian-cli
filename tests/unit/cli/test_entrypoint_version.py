"""Tests for root version fast path in the CLI entrypoint."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

import meridian.cli.entrypoint as entrypoint_module


def test_is_version_request_accepts_short_v_flag() -> None:
    assert entrypoint_module._is_version_request(["-v"]) is True
    assert entrypoint_module._is_version_request(["--version"]) is True


def test_is_version_request_rejects_v_when_command_follows() -> None:
    assert entrypoint_module._is_version_request(["-v", "spawn", "list"]) is False


def test_main_prints_version_for_short_v_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(sys, "argv", ["meridian", "-v"]):
        entrypoint_module.main()
    captured = capsys.readouterr()
    assert captured.out.startswith("meridian ")
    assert captured.err == ""


def test_main_validates_mode_before_version_fast_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch.object(sys, "argv", ["meridian", "--mode", "bogus", "--version"]),
        pytest.raises(SystemExit),
    ):
        entrypoint_module.main()
    captured = capsys.readouterr()
    assert not captured.out.startswith("meridian ")


def test_main_prints_version_with_valid_mode(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(sys, "argv", ["meridian", "--mode", "human", "--version"]):
        entrypoint_module.main()
    captured = capsys.readouterr()
    assert captured.out.startswith("meridian ")
