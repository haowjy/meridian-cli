"""Tests for the meridian-pi wrapper entrypoint."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from meridian.cli import pi_entrypoint


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


def test_main_sets_pi_agent_dir_and_passes_remaining_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["env"] = env
        captured["check"] = check
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(pi_entrypoint.subprocess, "run", fake_run)
    monkeypatch.setattr(pi_entrypoint, "_runner_path", lambda: tmp_path / "runner.mjs")
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "meridian-home"))
    monkeypatch.setattr(
        pi_entrypoint.sys,
        "argv",
        ["meridian-pi", "--agent-dir", str(tmp_path / "agent-dir"), "--help"],
    )

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 0
    assert captured["command"] == ["node", str(tmp_path / "runner.mjs"), "--help"]
    captured_env = captured["env"]
    assert isinstance(captured_env, dict)
    assert captured_env["PI_CODING_AGENT_DIR"] == str(tmp_path / "agent-dir")
    assert (tmp_path / "agent-dir" / "sessions").is_dir()


def test_main_reports_missing_node_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def fake_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        _ = command, env, check
        raise FileNotFoundError("node")

    monkeypatch.setattr(pi_entrypoint.subprocess, "run", fake_run)
    monkeypatch.setattr(pi_entrypoint, "_runner_path", lambda: tmp_path / "runner.mjs")
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--help"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 1
    assert "Node.js runtime is required" in capsys.readouterr().err


def test_main_reports_invalid_wrapper_flag(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(pi_entrypoint.sys, "argv", ["meridian-pi", "--agent-dir"])

    with pytest.raises(SystemExit) as raised:
        pi_entrypoint.main()

    assert raised.value.code == 2
    assert "--agent-dir requires" in capsys.readouterr().err
