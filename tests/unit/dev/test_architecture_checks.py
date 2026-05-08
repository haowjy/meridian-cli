"""Tests for the architecture-check lane entrypoint."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from meridian.dev import architecture_checks


def test_build_pytest_args_includes_lane_defaults_and_targets() -> None:
    args = architecture_checks.build_pytest_args(["-k", "launch"])

    assert args == [
        sys.executable,
        "-m",
        "pytest",
        *architecture_checks.DEFAULT_ARGS,
        *architecture_checks.ARCHITECTURE_TEST_TARGETS,
        "-k",
        "launch",
    ]


def test_main_returns_subprocess_exit_code(monkeypatch) -> None:
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], *, check: bool) -> SimpleNamespace:
        recorded["command"] = command
        recorded["check"] = check
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(architecture_checks.subprocess, "run", fake_run)

    result = architecture_checks.main(["-k", "factory"])

    assert result == 7
    assert recorded == {
        "command": architecture_checks.build_pytest_args(["-k", "factory"]),
        "check": False,
    }


def test_main_uses_sys_argv_when_no_explicit_args(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["architecture-check", "-k", "contracts"])
    monkeypatch.setattr(
        architecture_checks.subprocess,
        "run",
        lambda command, *, check: SimpleNamespace(returncode=0),
    )

    assert architecture_checks.main() == 0
