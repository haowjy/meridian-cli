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


def test_resolve_pi_runtime_uses_override_before_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

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
        calls.append(command)
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

    monkeypatch.setattr(subprocess, "run", _fake_run)

    resolved = resolve_pi_runtime(
        env={
            "PATH": "/nonexistent",
            "MERIDIAN_PI_BINARY": str(Path("~/custom/pi").expanduser()),
        },
        role="spawned",
    )

    assert resolved.binary_path == str(Path("~/custom/pi").expanduser())
    assert resolved.runtime_kind == "override"
    assert resolved.runtime_version == "pi 9.9.9"
    assert calls[0][0] == str(Path("~/custom/pi").expanduser())


def test_resolve_pi_runtime_uses_path_when_override_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    which_calls: list[tuple[str, str | None]] = []

    def _fake_which(binary_name: str, *, path: str | None = None) -> str:
        which_calls.append((binary_name, path))
        assert binary_name == "pi"
        return "/opt/pi/bin/pi"

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
            return _completed(command, stdout="pi 2.0.0\n")
        return _completed(
            command,
            stdout=(
                "--mode rpc --model --append-system-prompt --session --fork "
                "--session-dir --no-extensions --no-skills --no-context-files "
                "--no-prompt-templates -e --extension\n"
            ),
        )

    monkeypatch.setattr("meridian.lib.harness.pi_runtime_resolver.shutil.which", _fake_which)
    monkeypatch.setattr(subprocess, "run", _fake_run)

    resolved = resolve_pi_runtime(env={"PATH": "/path-pi"}, role="spawned")

    assert which_calls == [("pi", "/path-pi")]
    assert resolved.binary_path == "/opt/pi/bin/pi"
    assert resolved.runtime_kind == "path"
    assert resolved.runtime_version == "pi 2.0.0"


def test_resolve_pi_runtime_blank_override_falls_back_to_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    which_calls: list[tuple[str, str | None]] = []

    def _fake_which(binary_name: str, *, path: str | None = None) -> str:
        which_calls.append((binary_name, path))
        assert binary_name == "pi"
        return "/usr/local/bin/pi"

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
            return _completed(command, stdout="pi 4.0.0\n")
        return _completed(
            command,
            stdout=(
                "--mode rpc --model --append-system-prompt --session --fork "
                "--session-dir --no-extensions --no-skills --no-context-files "
                "--no-prompt-templates -e --extension\n"
            ),
        )

    monkeypatch.setattr("meridian.lib.harness.pi_runtime_resolver.shutil.which", _fake_which)
    monkeypatch.setattr(subprocess, "run", _fake_run)

    resolved = resolve_pi_runtime(
        env={"PATH": "/path-pi", "MERIDIAN_PI_BINARY": " \t "},
        role="spawned",
    )

    assert which_calls == [("pi", "/path-pi")]
    assert resolved.binary_path == "/usr/local/bin/pi"
    assert resolved.runtime_kind == "path"


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

    expected_path = str(Path("/tmp/pi"))
    assert str(exc_info.value) == (
        f"Installed Pi at {expected_path} is not compatible with Meridian's Pi harness: "
        "`--help` surface missing required flags: --mode, rpc, --append-system-prompt, "
        "--session, --fork, --session-dir/PI_CODING_AGENT_SESSION_DIR, --no-extensions, "
        "--no-skills, "
        "--no-context-files, --no-prompt-templates, -e/--extension.\n"
        "Run `pi update`, or set MERIDIAN_PI_BINARY=/path/to/pi to another compatible Pi binary."
    )


def test_resolve_pi_runtime_accepts_comma_separated_help_aliases(
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
        if command[1] == "--version":
            return _completed(command, stdout="pi 1.0.0\n")
        return _completed(
            command,
            stdout=(
                "--mode <mode> Output mode: text, json, or rpc\n"
                "--model <pattern>\n"
                "--append-system-prompt <text>\n"
                "--session <path|id>\n"
                "--fork <path|id>\n"
                "--session-dir <dir>\n"
                "--no-extensions, -ne Disable extension discovery\n"
                "--no-skills, -ns Disable skills discovery\n"
                "--no-context-files, -nc Disable AGENTS.md discovery\n"
                "--no-prompt-templates, -np Disable prompt templates\n"
                "--extension, -e <path> Load extension\n"
            ),
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    resolved = resolve_pi_runtime(
        env={"PATH": "/fake/path", "MERIDIAN_PI_BINARY": "/tmp/pi"},
        role="spawned",
    )

    assert resolved.binary_path == str(Path("/tmp/pi"))
