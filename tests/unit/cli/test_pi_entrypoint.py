"""Tests for the meridian-pi wrapper entrypoint."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from meridian.cli import pi_entrypoint


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
    env_default = {"MERIDIAN_HOME": str(tmp_path / "home")}
    default_dir = pi_entrypoint._resolve_agent_dir(None, env_default)

    assert default_dir == tmp_path / "home" / "pi" / "agent"

    env_override = {
        "MERIDIAN_HOME": str(tmp_path / "home"),
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


def test_compiled_binary_candidates_prefers_posix_binary_first() -> None:
    candidate_names = pi_entrypoint._compiled_binary_candidate_names("posix")

    assert candidate_names == ("meridian-pi", "meridian-pi.exe")


def test_compiled_binary_candidates_prefers_windows_binary_first() -> None:
    candidate_names = pi_entrypoint._compiled_binary_candidate_names("nt")

    assert candidate_names == ("meridian-pi.exe", "meridian-pi")


def test_main_sets_pi_agent_dir_and_passes_remaining_args(
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
    monkeypatch.setattr(pi_entrypoint, "_runner_path", lambda: runner_path)
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "meridian-home"))
    monkeypatch.setattr(
        pi_entrypoint.sys,
        "argv",
        ["meridian-pi", "--agent-dir", str(tmp_path / "agent-dir"), "--help"],
    )

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 0
    assert captured["command"] == ["node", str(runner_path), "--help"]
    captured_env = captured["env"]
    assert isinstance(captured_env, dict)
    assert captured_env["PI_CODING_AGENT_DIR"] == str(tmp_path / "agent-dir")
    assert (tmp_path / "agent-dir" / "sessions").is_dir()


def test_main_prefers_packaged_binary_when_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
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
        "_compiled_binary_candidates",
        lambda: (packaged_binary,),
    )
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 0
    assert captured["command"] == [str(packaged_binary), "--help"]


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
    monkeypatch.setattr(
        pi_entrypoint,
        "_compiled_binary_candidates",
        lambda: (tmp_path / "ignored-packaged-binary",),
    )
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 0
    assert captured["command"] == [str(override_binary), "--help"]


def test_main_falls_back_to_node_runner_when_packaged_binary_absent(
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
    assert captured["command"] == ["node", str(runner_path), "--help"]


def test_main_reports_missing_compiled_runtime_and_source_fallback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    runner_path = tmp_path / "missing-runner.mjs"

    def should_not_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"unexpected subprocess invocation: {command!r}, {env!r}, {check!r}")

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
    assert "compiled meridian-pi runtime is not installed" in stderr
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
    monkeypatch.setattr(pi_entrypoint, "_runner_path", lambda: runner_path)
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
    stderr = capsys.readouterr().err
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

    monkeypatch.setattr(pi_entrypoint, "_compiled_binary_candidates", lambda: (packaged_binary,))
    monkeypatch.setattr(pi_entrypoint.subprocess, "run", should_not_run)
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 1
    stderr = capsys.readouterr().err
    assert "present but not executable on this host" in stderr
    assert str(packaged_binary) in stderr


def test_main_reports_invalid_wrapper_flag(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--agent-dir"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 2
    assert "--agent-dir requires" in capsys.readouterr().err
