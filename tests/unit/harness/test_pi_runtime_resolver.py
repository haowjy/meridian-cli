# qa-validated: pi-rpc-quiescence
"""Pi runtime resolution/probe tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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

    def _fake_which(binary_name: str, *, path: str | None = None) -> str | None:
        which_calls.append((binary_name, path))
        assert binary_name == "pi"
        return which_result

    def _fake_run(
        command: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        env: dict[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        _ = check, capture_output, text, env, timeout
        run_calls.append(command)
        if command[1] == "--version":
            return _completed(command, stdout="pi 9.9.9\n")
        return _completed(
            command,
            stdout=(
                "--mode rpc --model --append-system-prompt --session --fork "
                "--session-dir --no-extensions --no-skills "
                "--no-context-files --no-prompt-templates -e --extension\n"
            ),
        )

    monkeypatch.setattr("meridian.lib.harness.pi_runtime_resolver.shutil.which", _fake_which)
    monkeypatch.setattr(subprocess, "run", _fake_run)

    resolved = resolve_pi_runtime(env=env, role="spawned")

    assert which_calls == expected_which_calls
    assert resolved.binary_path == expected_binary
    assert resolved.runtime_kind == expected_kind
    assert resolved.runtime_version == "pi 9.9.9"
    assert run_calls[0][0] == expected_binary


def test_resolve_pi_runtime_never_uses_legacy_node_bun_or_meridian_pi_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    which_calls: list[str] = []

    def _fake_which(binary_name: str, *, path: str | None = None) -> str | None:
        _ = path
        which_calls.append(binary_name)
        if binary_name == "pi":
            return None
        if binary_name in {"node", "bun", "meridian-pi"}:
            return f"/fake/{binary_name}"
        return None

    def _never_run_subprocess(
        command: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        env: dict[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        _ = command, check, capture_output, text, env, timeout
        raise AssertionError("subprocess.run should not be called when no installed pi exists")

    monkeypatch.setattr("meridian.lib.harness.pi_runtime_resolver.shutil.which", _fake_which)
    monkeypatch.setattr(subprocess, "run", _never_run_subprocess)

    with pytest.raises(PiRuntimeResolutionError) as exc_info:
        resolve_pi_runtime(
            env={
                "PATH": "/empty",
                "MERIDIAN_PI_ALLOW_BUNDLED_FALLBACK": "1",
                "MERIDIAN_PI_BUNDLED_AUTH_CONFIRMED": "1",
                "MERIDIAN_PI_NODE_BIN": "/fake/node",
            },
            role="spawned",
        )

    assert which_calls == ["pi"]
    assert str(exc_info.value) == (
        "Pi is not installed or not on PATH.\n"
        "Install Pi using the official Pi instructions, then run `pi --version` and retry.\n"
        "Set MERIDIAN_PI_BINARY=/path/to/pi to use a non-PATH installation."
    )


@pytest.mark.parametrize(
    ("probe_error", "expected_detail"),
    [
        pytest.param(FileNotFoundError(), "binary not found", id="file-not-found"),
        pytest.param(OSError("permission denied"), "permission denied", id="os-error"),
        pytest.param(
            subprocess.TimeoutExpired(cmd=["/bad/pi", "--version"], timeout=2.0),
            "timed out",
            id="timeout",
        ),
        pytest.param(
            subprocess.SubprocessError("probe failed"),
            "probe failed",
            id="subprocess-error",
        ),
    ],
)
def test_resolve_pi_runtime_probe_exceptions_report_execution_guidance(
    monkeypatch: pytest.MonkeyPatch,
    probe_error: BaseException,
    expected_detail: str,
) -> None:
    def _fake_run(
        command: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        env: dict[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        _ = command, check, capture_output, text, env, timeout
        raise probe_error

    monkeypatch.setattr(subprocess, "run", _fake_run)

    with pytest.raises(PiRuntimeResolutionError) as exc_info:
        resolve_pi_runtime(
            env={"PATH": "/ignored", "MERIDIAN_PI_BINARY": "/bad/pi"},
            role="spawned",
        )

    message = str(exc_info.value)
    expected_path = str(Path("/bad/pi"))
    assert message.startswith(f"Unable to execute Pi at {expected_path}:")
    assert expected_detail in message
    assert "not compatible" not in message
    assert "Run `pi update`" not in message
    assert "run `pi --version`" in message
    assert "MERIDIAN_PI_BINARY=/path/to/pi" in message


def test_resolve_pi_runtime_nonzero_version_probe_reports_probe_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(
        command: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        env: dict[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        _ = check, capture_output, text, env, timeout
        expected_path = str(Path("/bad/pi"))
        assert command == [expected_path, "--version"]
        return _completed(command, returncode=127, stderr="cannot execute")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    with pytest.raises(PiRuntimeResolutionError) as exc_info:
        resolve_pi_runtime(
            env={"PATH": "/ignored", "MERIDIAN_PI_BINARY": "/bad/pi"},
            role="spawned",
        )

    message = str(exc_info.value)
    expected_path = str(Path("/bad/pi"))
    assert message.startswith(f"Unable to execute Pi at {expected_path}:")
    assert "`--version` probe failed: cannot execute" in message
    assert "Run `pi update`" not in message
    assert "MERIDIAN_PI_BINARY=/path/to/pi" in message


def test_resolve_pi_runtime_incompatible_help_surface_reports_update_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary_path = "/tmp/pi"

    def _fake_which(binary_name: str, *, path: str | None = None) -> str:
        _ = path
        assert binary_name == "pi"
        return binary_path

    def _fake_run(
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

    monkeypatch.setattr("meridian.lib.harness.pi_runtime_resolver.shutil.which", _fake_which)
    monkeypatch.setattr(subprocess, "run", _fake_run)

    with pytest.raises(PiRuntimeResolutionError) as exc_info:
        resolve_pi_runtime(env={"PATH": "/test/path"}, role="spawned")

    # The resolver preserves the raw path from shutil.which() in error messages
    # (no Path normalization), so match the exact string.
    assert str(exc_info.value) == (
        f"Installed Pi at {binary_path} is not compatible with Meridian's Pi harness: "
        "`--help` surface missing required flags: --mode, rpc, --append-system-prompt, "
        "--session, --fork, --session-dir/PI_CODING_AGENT_SESSION_DIR, --no-extensions, "
        "--no-skills, "
        "--no-context-files, --no-prompt-templates, -e/--extension.\n"
        "Run `pi update`, or set MERIDIAN_PI_BINARY=/path/to/pi to another compatible Pi binary."
    )
