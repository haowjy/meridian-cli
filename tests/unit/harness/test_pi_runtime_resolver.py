# qa-validated: pi-rpc-quiescence
"""Focused Pi runtime resolution/probe coverage."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from meridian.lib.harness import pi_runtime_resolver
from meridian.lib.harness.pi_runtime_resolver import (
    PiRuntimeResolutionError,
    resolve_pi_runtime,
)


def _completed(
    command: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)


def _spawned_help_surface() -> str:
    return (
        "--mode rpc --model --append-system-prompt --session --fork "
        "--session-dir --no-extensions --no-skills "
        "--no-context-files --no-prompt-templates -e --extension\n"
    )


def test_resolve_pi_runtime_missing_path_reports_install_guidance() -> None:
    with pytest.raises(PiRuntimeResolutionError) as exc_info:
        resolve_pi_runtime(env={"PATH": ""}, role="spawned")

    assert str(exc_info.value) == (
        "Pi is not installed or not on PATH.\n"
        "Install Pi using the official Pi instructions, then run `pi --version` and retry.\n"
        "Set MERIDIAN_PI_BINARY=/path/to/pi to use a non-PATH installation."
    )


@pytest.mark.parametrize(
    ("env", "which_result", "expected_binary", "expected_kind", "expected_which_calls"),
    [
        (
            {"PATH": "/nonexistent", "MERIDIAN_PI_BINARY": str(Path("~/custom/pi").expanduser())},
            None,
            str(Path("~/custom/pi").expanduser()),
            "override",
            [],
        ),
        ({"PATH": "/path-pi"}, "/opt/pi/bin/pi", "/opt/pi/bin/pi", "path", [("pi", "/path-pi")]),
        (
            {"PATH": "/path-pi", "MERIDIAN_PI_BINARY": " \t "},
            "/usr/local/bin/pi",
            "/usr/local/bin/pi",
            "path",
            [("pi", "/path-pi")],
        ),
    ],
)
def test_resolve_pi_runtime_selection_precedence(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    which_result: str | None,
    expected_binary: str,
    expected_kind: str,
    expected_which_calls: list[tuple[str, str | None]],
) -> None:
    which_calls: list[tuple[str, str | None]] = []
    run_calls: list[list[str]] = []

    def fake_which(binary_name: str, *, path: str | None = None) -> str | None:
        which_calls.append((binary_name, path))
        return which_result

    def fake_run(
        command: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        env: dict[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        assert capture_output is True
        assert text is True
        assert timeout == 2.0
        _ = env
        run_calls.append(command)
        if command[1] == "--version":
            return _completed(command, stdout="pi 9.9.9\n")
        return _completed(command, stdout=_spawned_help_surface())

    monkeypatch.setattr(pi_runtime_resolver.shutil, "which", fake_which)
    monkeypatch.setattr(pi_runtime_resolver.subprocess, "run", fake_run)

    resolved = resolve_pi_runtime(env=env, role="spawned")

    assert which_calls == expected_which_calls
    assert resolved.binary_path == expected_binary
    assert resolved.runtime_kind == expected_kind
    assert resolved.runtime_version == "pi 9.9.9"
    assert run_calls == [[expected_binary, "--version"], [expected_binary, "--help"]]


def test_resolve_pi_runtime_incompatible_help_surface_reports_update_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary_path = "/tmp/pi"

    monkeypatch.setattr(
        pi_runtime_resolver.shutil,
        "which",
        lambda _name, *, path=None: binary_path,
    )

    def fake_run(
        command: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        env: dict[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        _ = check, capture_output, text, env, timeout
        if command[1] == "--version":
            return _completed(command, stdout="pi 1.0.0\n")
        return _completed(command, stdout="--model only\n")

    monkeypatch.setattr(pi_runtime_resolver.subprocess, "run", fake_run)

    with pytest.raises(PiRuntimeResolutionError) as exc_info:
        resolve_pi_runtime(env={"PATH": "/test/path"}, role="spawned")

    assert str(exc_info.value) == (
        f"Installed Pi at {binary_path} is not compatible with Meridian's Pi harness: "
        "`--help` surface missing required flags: --mode, rpc, --append-system-prompt, "
        "--session, --fork, --session-dir/PI_CODING_AGENT_SESSION_DIR, --no-extensions, "
        "--no-skills, "
        "--no-context-files, --no-prompt-templates, -e/--extension.\n"
        "Run `pi update`, or set MERIDIAN_PI_BINARY=/path/to/pi to another compatible Pi binary."
    )
