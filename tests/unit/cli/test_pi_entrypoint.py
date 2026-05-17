# qa-validated: pi-rpc-quiescence
"""Tests for the meridian-pi wrapper entrypoint."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from meridian.cli import pi_entrypoint

_REAL_PROBE_RUNTIME_COMPATIBILITY = pi_entrypoint._probe_runtime_compatibility


@pytest.fixture(autouse=True)
def _disable_runtime_diag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pi_entrypoint, "_log_runtime_diagnostics", lambda **_: None)
    monkeypatch.setattr(
        pi_entrypoint,
        "_runtime_version",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        pi_entrypoint,
        "_probe_runtime_compatibility",
        lambda *_args, **_kwargs: None,
    )


def _create_source_runtime_layout(runtime_dir: Path) -> Path:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    runner_path = runtime_dir / "runner.mjs"
    runner_path.write_text("// runner")
    (runtime_dir / "node_modules" / "@earendil-works" / "pi-coding-agent").mkdir(
        parents=True,
        exist_ok=True,
    )
    return runner_path


def test_strip_agent_dir_flag_removes_wrapper_only_flag() -> None:
    passthrough, agent_dir = pi_entrypoint._strip_agent_dir_flag(
        [
            "-p",
            "--agent-dir",
            "~/custom-agent-dir",
            "--mode",
            "json",
            "prompt",
        ]
    )

    assert passthrough == ["-p", "--mode", "json", "prompt"]
    assert agent_dir == "~/custom-agent-dir"


def test_strip_agent_dir_flag_supports_equals_syntax() -> None:
    passthrough, agent_dir = pi_entrypoint._strip_agent_dir_flag(
        ["--agent-dir=/tmp/agent", "--help"]
    )

    assert passthrough == ["--help"]
    assert agent_dir == "/tmp/agent"


@pytest.mark.parametrize("argv", [["--agent-dir"], ["--agent-dir="]])
def test_strip_agent_dir_flag_requires_value(argv: list[str]) -> None:
    with pytest.raises(ValueError, match="--agent-dir requires"):
        pi_entrypoint._strip_agent_dir_flag(argv)


def test_resolve_agent_dir_precedence(tmp_path: Path) -> None:
    default_dir = pi_entrypoint._resolve_agent_dir(None, {})
    assert default_dir is None

    env_override = {
        "MERIDIAN_PI_AGENT_DIR": str(tmp_path / "env-override"),
    }
    env_dir = pi_entrypoint._resolve_agent_dir(None, env_override)

    assert env_dir == tmp_path / "env-override"

    cli_dir = pi_entrypoint._resolve_agent_dir(str(tmp_path / "cli-override"), env_override)

    assert cli_dir == tmp_path / "cli-override"


def test_ensure_agent_dir_layout_creates_required_subdirectories(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent"

    pi_entrypoint._ensure_agent_dir_layout(agent_dir)

    assert agent_dir.is_dir()
    assert (agent_dir / "sessions").is_dir()
    assert (agent_dir / "extensions").is_dir()
    assert (agent_dir / "bin").is_dir()


def test_resolve_session_dir_defaults_to_meridian_pi_sessions(tmp_path: Path) -> None:
    session_dir = pi_entrypoint._resolve_session_dir({})
    assert Path(session_dir).parts[-2:] == ("meridian-pi", "sessions")

    env_override = {"PI_CODING_AGENT_SESSION_DIR": str(tmp_path / "custom-sessions")}
    assert pi_entrypoint._resolve_session_dir(env_override) == tmp_path / "custom-sessions"


def test_compiled_binary_candidates_prefers_posix_binary_first() -> None:
    candidate_names = pi_entrypoint._compiled_binary_candidate_names("posix")

    assert candidate_names == ("meridian-pi", "meridian-pi.exe")


def test_compiled_binary_candidates_prefers_windows_binary_first() -> None:
    candidate_names = pi_entrypoint._compiled_binary_candidate_names("nt")

    assert candidate_names == ("meridian-pi.exe", "meridian-pi")


def test_main_sets_session_dir_and_optional_agent_dir_and_passes_remaining_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    installed_pi = tmp_path / "pi"
    installed_pi.write_text("binary")
    installed_pi.chmod(0o755)

    def fake_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["env"] = env
        captured["check"] = check
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(pi_entrypoint.subprocess, "run", fake_run)
    monkeypatch.setattr(
        pi_entrypoint,
        "_resolve_installed_pi_binary",
        lambda env: str(installed_pi),
    )
    monkeypatch.setattr(
        pi_entrypoint.sys,
        "argv",
        ["meridian-pi", "--agent-dir", str(tmp_path / "agent-dir"), "--help"],
    )

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 0
    captured_command = captured["command"]
    assert isinstance(captured_command, list)
    assert captured_command[0] == str(installed_pi)
    assert "--session-dir" in captured_command
    assert captured_command[-1] == "--help"
    captured_env = captured["env"]
    assert isinstance(captured_env, dict)
    assert captured_env["PI_CODING_AGENT_DIR"] == str(tmp_path / "agent-dir")
    assert Path(captured_env["PI_CODING_AGENT_SESSION_DIR"]).parts[-2:] == (
        "meridian-pi",
        "sessions",
    )
    assert (tmp_path / "agent-dir" / "sessions").is_dir()


def test_main_emits_runtime_selected_event(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    captured_diag: dict[str, object] = {}
    captured_run: dict[str, object] = {}
    runtime_version = "pi 1.2.3"
    installed_pi = tmp_path / "pi"
    installed_pi.write_text("binary")
    installed_pi.chmod(0o755)

    def fake_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        _ = command, check
        captured_run["env"] = env
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(pi_entrypoint.subprocess, "run", fake_run)
    monkeypatch.setattr(
        pi_entrypoint,
        "_resolve_installed_pi_binary",
        lambda env: str(installed_pi),
    )
    monkeypatch.setattr(
        pi_entrypoint,
        "_runtime_version",
        lambda _command, _env: runtime_version,
    )
    monkeypatch.setattr(
        pi_entrypoint,
        "_log_runtime_diagnostics",
        lambda **kwargs: captured_diag.update(kwargs),
    )
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 0
    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    assert payloads
    selected = payloads[0]
    assert selected["type"] == "meridian.pi.runtime.selected"
    assert selected["runtime_kind"] == "installed"
    assert selected["runtime_path"] == str(installed_pi)
    assert selected["auth_policy"] == "inherit-runtime-default-auth-config"
    assert selected["runtime_version"] == runtime_version
    assert Path(str(selected["session_dir"])).parts[-2:] == ("meridian-pi", "sessions")
    assert captured_diag["runtime_version"] == runtime_version
    captured_env = captured_run["env"]
    assert isinstance(captured_env, dict)
    assert "PI_CODING_AGENT_DIR" not in captured_env
    assert Path(captured_env["PI_CODING_AGENT_SESSION_DIR"]).parts[-2:] == (
        "meridian-pi",
        "sessions",
    )


def test_main_emits_runtime_selected_event_with_explicit_agent_dir_auth_policy(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    captured_diag: dict[str, object] = {}
    installed_pi = tmp_path / "pi"
    installed_pi.write_text("binary")
    installed_pi.chmod(0o755)
    agent_dir = tmp_path / "agent-dir"

    def fake_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        _ = command, env, check
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(pi_entrypoint.subprocess, "run", fake_run)
    monkeypatch.setattr(
        pi_entrypoint,
        "_resolve_installed_pi_binary",
        lambda env: str(installed_pi),
    )
    monkeypatch.setattr(
        pi_entrypoint,
        "_runtime_version",
        lambda _command, _env: "pi 1.2.3",
    )
    monkeypatch.setattr(
        pi_entrypoint,
        "_log_runtime_diagnostics",
        lambda **kwargs: captured_diag.update(kwargs),
    )
    monkeypatch.setattr(
        pi_entrypoint.sys,
        "argv",
        ["meridian-pi", "--agent-dir", str(agent_dir), "--help"],
    )

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 0
    payloads = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert payloads
    selected = payloads[0]
    assert selected["type"] == "meridian.pi.runtime.selected"
    assert selected["runtime_kind"] == "installed"
    assert selected["auth_policy"] == "explicit-agent-dir-override"
    assert captured_diag["agent_dir_policy"] == "explicit-agent-dir-override"
    assert (agent_dir / "sessions").is_dir()
    assert (agent_dir / "extensions").is_dir()
    assert (agent_dir / "bin").is_dir()


def test_main_emits_unknown_runtime_version_when_probe_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    installed_pi = tmp_path / "pi"
    installed_pi.write_text("binary")
    installed_pi.chmod(0o755)

    def fake_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        _ = command, env, check
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(pi_entrypoint.subprocess, "run", fake_run)
    monkeypatch.setattr(
        pi_entrypoint,
        "_resolve_installed_pi_binary",
        lambda env: str(installed_pi),
    )
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 0
    payloads = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert payloads
    assert payloads[0]["runtime_version"] == "unknown"


def test_main_prefers_binary_override_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    override_binary = tmp_path / "override-pi"
    override_binary.write_text("binary")
    override_binary.chmod(0o755)

    def fake_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["env"] = env
        captured["check"] = check
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(pi_entrypoint.subprocess, "run", fake_run)
    monkeypatch.setenv("MERIDIAN_PI_BINARY", str(override_binary))
    monkeypatch.setattr(pi_entrypoint, "_resolve_installed_pi_binary", lambda env: "/path/to/pi")
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 0
    captured_command = captured["command"]
    assert isinstance(captured_command, list)
    assert captured_command[0] == str(override_binary)
    assert "--help" in captured_command


def test_main_prefers_installed_pi_over_packaged_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    installed_pi = tmp_path / "pi"
    installed_pi.write_text("binary")
    installed_pi.chmod(0o755)
    packaged_binary = tmp_path / "meridian-pi"
    packaged_binary.write_text("binary")
    packaged_binary.chmod(0o755)

    def fake_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["env"] = env
        captured["check"] = check
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(pi_entrypoint.subprocess, "run", fake_run)
    monkeypatch.setattr(
        pi_entrypoint,
        "_resolve_installed_pi_binary",
        lambda env: str(installed_pi),
    )
    monkeypatch.setattr(pi_entrypoint, "_compiled_binary_candidates", lambda: (packaged_binary,))
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 0
    captured_command = captured["command"]
    assert isinstance(captured_command, list)
    assert captured_command[0] == str(installed_pi)


def test_main_reports_incompatible_installed_pi_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    installed_pi = tmp_path / "pi"
    installed_pi.write_text("binary")
    installed_pi.chmod(0o755)

    def should_not_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"unexpected subprocess invocation: {command!r}, {env!r}, {check!r}")

    monkeypatch.setattr(
        pi_entrypoint,
        "_resolve_installed_pi_binary",
        lambda env: str(installed_pi),
    )
    monkeypatch.setattr(
        pi_entrypoint,
        "_probe_runtime_compatibility",
        lambda _binary_path, _env: "exit 2",
    )
    monkeypatch.setattr(pi_entrypoint.subprocess, "run", should_not_run)
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 1
    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    assert payloads
    assert payloads[0]["type"] == "meridian.pi.runtime.error"
    assert payloads[0]["phase"] == "preflight"
    assert "exit 2" in str(payloads[0]["error"])
    assert str(installed_pi) in str(payloads[0]["error"])
    stderr = captured.err
    assert "installed `pi` runtime failed compatibility probe" in stderr
    assert str(installed_pi) in stderr
    assert "exit 2" in stderr
    assert "MERIDIAN_PI_ALLOW_BUNDLED_FALLBACK=1" in stderr


def test_main_rejects_version_only_runtime_without_required_help_flags(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    installed_pi = tmp_path / "pi"
    installed_pi.write_text("binary")
    installed_pi.chmod(0o755)

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
            return subprocess.CompletedProcess(command, 0, stdout="pi 0.0.0", stderr="")
        if command[1] == "--help":
            return subprocess.CompletedProcess(command, 0, stdout="usage: pi [options]", stderr="")
        raise AssertionError(f"unexpected compatibility probe command: {command!r}")

    monkeypatch.setattr(pi_entrypoint.subprocess, "run", fake_run)
    monkeypatch.setattr(
        pi_entrypoint,
        "_resolve_installed_pi_binary",
        lambda env: str(installed_pi),
    )
    monkeypatch.setattr(
        pi_entrypoint,
        "_probe_runtime_compatibility",
        _REAL_PROBE_RUNTIME_COMPATIBILITY,
    )
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 1
    stderr = capsys.readouterr().err
    assert "failed compatibility probe" in stderr
    assert "`--help` surface missing required flags" in stderr


def test_main_requires_explicit_opt_in_for_bundled_fallback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def should_not_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"unexpected subprocess invocation: {command!r}, {env!r}, {check!r}")

    monkeypatch.setattr(pi_entrypoint, "_resolve_installed_pi_binary", lambda env: None)
    monkeypatch.setattr(pi_entrypoint.subprocess, "run", should_not_run)
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 1
    stderr = capsys.readouterr().err
    assert "no compatible installed `pi` runtime found on PATH" in stderr
    assert "MERIDIAN_PI_ALLOW_BUNDLED_FALLBACK=1" in stderr


def test_main_uses_node_fallback_when_explicitly_opted_in(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    runner_path = _create_source_runtime_layout(tmp_path / "pi-runtime")

    def fake_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["env"] = env
        captured["check"] = check
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(pi_entrypoint.subprocess, "run", fake_run)
    monkeypatch.setattr(pi_entrypoint, "_resolve_installed_pi_binary", lambda env: None)
    monkeypatch.setenv("MERIDIAN_PI_ALLOW_BUNDLED_FALLBACK", "1")
    monkeypatch.setenv("MERIDIAN_PI_BUNDLED_AUTH_CONFIRMED", "1")
    monkeypatch.setattr(pi_entrypoint, "_runner_path", lambda: runner_path)
    monkeypatch.setattr(
        pi_entrypoint,
        "_compiled_binary_candidates",
        lambda: (tmp_path / "missing-binary",),
    )
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 0
    captured_command = captured["command"]
    assert isinstance(captured_command, list)
    assert captured_command[0:2] == ["node", str(runner_path)]
    assert "--help" in captured_command


def test_main_reports_missing_compiled_runtime_when_explicit_fallback_requested(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    runner_path = tmp_path / "missing-runner.mjs"

    def should_not_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"unexpected subprocess invocation: {command!r}, {env!r}, {check!r}")

    monkeypatch.setattr(pi_entrypoint, "_resolve_installed_pi_binary", lambda env: None)
    monkeypatch.setenv("MERIDIAN_PI_ALLOW_BUNDLED_FALLBACK", "1")
    monkeypatch.setenv("MERIDIAN_PI_BUNDLED_AUTH_CONFIRMED", "1")
    monkeypatch.setattr(pi_entrypoint, "_runner_path", lambda: runner_path)
    monkeypatch.setattr(pi_entrypoint.subprocess, "run", should_not_run)
    monkeypatch.setattr(
        pi_entrypoint,
        "_compiled_binary_candidates",
        lambda: (tmp_path / "missing-binary",),
    )
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 1
    stderr = capsys.readouterr().err
    assert "bundled/dev fallback requested but runtime is unavailable" in stderr
    assert "build-meridian-pi-runtime.sh" in stderr


def test_main_reports_missing_node_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    runner_path = _create_source_runtime_layout(tmp_path / "pi-runtime")

    def fake_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        _ = command, env, check
        raise FileNotFoundError("node")

    monkeypatch.setattr(pi_entrypoint.subprocess, "run", fake_run)
    monkeypatch.setattr(pi_entrypoint, "_resolve_installed_pi_binary", lambda env: None)
    monkeypatch.setenv("MERIDIAN_PI_ALLOW_BUNDLED_FALLBACK", "1")
    monkeypatch.setenv("MERIDIAN_PI_BUNDLED_AUTH_CONFIRMED", "1")
    monkeypatch.setattr(pi_entrypoint, "_runner_path", lambda: runner_path)
    monkeypatch.setattr(
        pi_entrypoint,
        "_compiled_binary_candidates",
        lambda: (tmp_path / "missing-packaged-binary",),
    )
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 1
    assert "Node.js runtime is required" in capsys.readouterr().err


def test_main_reports_missing_binary_override(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        _ = command, env, check
        raise FileNotFoundError("missing-pi-binary")

    monkeypatch.setattr(pi_entrypoint.subprocess, "run", fake_run)
    monkeypatch.setenv("MERIDIAN_PI_BINARY", "/missing/meridian-pi")
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 1
    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    assert payloads
    assert payloads[-1]["type"] == "meridian.pi.runtime.error"
    assert payloads[-1]["phase"] == "exec"
    assert payloads[-1]["runtime_kind"] == "override"
    assert payloads[-1]["runtime_path"] == "/missing/meridian-pi"
    stderr = captured.err
    assert "Pi runtime binary not found" in stderr
    assert "Node.js runtime is required" not in stderr


def test_main_reports_binary_override_execution_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def fake_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        _ = command, env, check
        raise PermissionError("permission denied")

    binary_override = tmp_path / "override-pi"
    binary_override.write_text("binary")

    monkeypatch.setattr(pi_entrypoint.subprocess, "run", fake_run)
    monkeypatch.setenv("MERIDIAN_PI_BINARY", str(binary_override))
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 1
    stderr = capsys.readouterr().err
    assert "failed to execute Pi runtime binary" in stderr
    assert "Node.js runtime is required" not in stderr


def test_main_fails_fast_for_non_executable_packaged_binary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    packaged_binary = tmp_path / "meridian-pi"
    packaged_binary.write_text("binary")
    packaged_binary.chmod(0o644)

    def should_not_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"unexpected subprocess invocation: {command!r}, {env!r}, {check!r}")

    monkeypatch.setattr(pi_entrypoint, "_resolve_installed_pi_binary", lambda env: None)
    monkeypatch.setenv("MERIDIAN_PI_ALLOW_BUNDLED_FALLBACK", "1")
    monkeypatch.setenv("MERIDIAN_PI_BUNDLED_AUTH_CONFIRMED", "1")
    monkeypatch.setattr(pi_entrypoint, "_compiled_binary_candidates", lambda: (packaged_binary,))
    monkeypatch.setattr(pi_entrypoint.subprocess, "run", should_not_run)
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 1
    stderr = capsys.readouterr().err
    assert "present but not executable on this host" in stderr
    assert str(packaged_binary) in stderr


def test_main_reports_incompatible_packaged_runtime_when_fallback_enabled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    packaged_binary = tmp_path / "meridian-pi"
    packaged_binary.write_text("binary")
    packaged_binary.chmod(0o755)

    def should_not_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"unexpected subprocess invocation: {command!r}, {env!r}, {check!r}")

    monkeypatch.setattr(pi_entrypoint, "_resolve_installed_pi_binary", lambda env: None)
    monkeypatch.setenv("MERIDIAN_PI_ALLOW_BUNDLED_FALLBACK", "1")
    monkeypatch.setenv("MERIDIAN_PI_BUNDLED_AUTH_CONFIRMED", "1")
    monkeypatch.setattr(pi_entrypoint, "_compiled_binary_candidates", lambda: (packaged_binary,))
    monkeypatch.setattr(
        pi_entrypoint,
        "_probe_runtime_compatibility",
        lambda _binary_path, _env: "unsupported glibc",
    )
    monkeypatch.setattr(pi_entrypoint.subprocess, "run", should_not_run)
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 1
    stderr = capsys.readouterr().err
    assert "bundled `meridian-pi` runtime failed compatibility probe" in stderr
    assert "unsupported glibc" in stderr


def test_main_requires_bundled_auth_confirmation_for_fallback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def should_not_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"unexpected subprocess invocation: {command!r}, {env!r}, {check!r}")

    monkeypatch.setattr(pi_entrypoint, "_resolve_installed_pi_binary", lambda env: None)
    monkeypatch.setenv("MERIDIAN_PI_ALLOW_BUNDLED_FALLBACK", "1")
    monkeypatch.setattr(pi_entrypoint.subprocess, "run", should_not_run)
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 1
    stderr = capsys.readouterr().err
    assert "bundled/dev fallback requires explicit auth/config confirmation" in stderr
    assert "MERIDIAN_PI_BUNDLED_AUTH_CONFIRMED=1" in stderr


def test_probe_runtime_compatibility_accepts_required_help_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            return subprocess.CompletedProcess(command, 0, stdout="pi 1.2.3", stderr="")
        if command[1] == "--help":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "usage: pi --mode rpc --session-dir PATH --no-extensions "
                    "--extension EXT"
                ),
                stderr="",
            )
        raise AssertionError(f"unexpected command: {command!r}")

    monkeypatch.setattr(pi_entrypoint.subprocess, "run", fake_run)

    assert _REAL_PROBE_RUNTIME_COMPATIBILITY("/fake/pi", {}) is None


def test_main_uses_packaged_fallback_when_installed_pi_is_incompatible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    installed_pi = tmp_path / "pi"
    installed_pi.write_text("binary")
    installed_pi.chmod(0o755)
    packaged_binary = tmp_path / "meridian-pi"
    packaged_binary.write_text("binary")
    packaged_binary.chmod(0o755)

    def fake_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["env"] = env
        captured["check"] = check
        return subprocess.CompletedProcess(command, 0)

    def compatibility_probe(binary_path: str, _env: dict[str, str]) -> str | None:
        if binary_path == str(installed_pi):
            return "exit 2"
        return None

    monkeypatch.setattr(pi_entrypoint.subprocess, "run", fake_run)
    monkeypatch.setattr(
        pi_entrypoint,
        "_resolve_installed_pi_binary",
        lambda env: str(installed_pi),
    )
    monkeypatch.setenv("MERIDIAN_PI_ALLOW_BUNDLED_FALLBACK", "1")
    monkeypatch.setenv("MERIDIAN_PI_BUNDLED_AUTH_CONFIRMED", "1")
    monkeypatch.setattr(pi_entrypoint, "_compiled_binary_candidates", lambda: (packaged_binary,))
    monkeypatch.setattr(pi_entrypoint, "_probe_runtime_compatibility", compatibility_probe)
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 0
    captured_command = captured["command"]
    assert isinstance(captured_command, list)
    assert captured_command[0] == str(packaged_binary)


def test_main_reports_rejected_installed_runtime_when_fallback_auth_not_confirmed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    installed_pi = tmp_path / "pi"
    installed_pi.write_text("binary")
    installed_pi.chmod(0o755)

    def should_not_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"unexpected subprocess invocation: {command!r}, {env!r}, {check!r}")

    monkeypatch.setattr(
        pi_entrypoint,
        "_resolve_installed_pi_binary",
        lambda env: str(installed_pi),
    )
    monkeypatch.setattr(
        pi_entrypoint,
        "_probe_runtime_compatibility",
        lambda _binary_path, _env: "exit 2",
    )
    monkeypatch.setenv("MERIDIAN_PI_ALLOW_BUNDLED_FALLBACK", "1")
    monkeypatch.setattr(pi_entrypoint.subprocess, "run", should_not_run)
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 1
    stderr = capsys.readouterr().err
    assert "bundled/dev fallback requires explicit auth/config confirmation" in stderr
    assert "installed `pi` runtime failed compatibility probe" in stderr


def test_main_reports_invalid_wrapper_flag(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--agent-dir"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 2
    assert "--agent-dir requires" in capsys.readouterr().err
