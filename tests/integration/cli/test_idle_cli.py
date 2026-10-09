"""CLI contract tests for the adapter-facing idle command group."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _idle_env(tmp_path: Path, *, role: str | None = "primary") -> dict[str, str]:
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("MERIDIAN_") or key == "_MERIDIAN_HARNESS":
            env.pop(key, None)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env.update(
        HOME=home.as_posix(),
        MERIDIAN_HOME=(home / ".meridian").as_posix(),
        MERIDIAN_NOTIFY_PUSH_BACKEND="none",
        MERIDIAN_NOTIFY_EMAIL_BACKEND="none",
        _MERIDIAN_HARNESS="claude",
    )
    if role is not None:
        env["MERIDIAN_SESSION_ROLE"] = role
    return env


def _run(
    tmp_path: Path,
    env: dict[str, str],
    *args: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "meridian", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _json_success(
    tmp_path: Path,
    env: dict[str, str],
    *args: str,
) -> object:
    result = _run(tmp_path, env, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""
    return json.loads(result.stdout)


@pytest.mark.integration
def test_idle_cli_round_trip(tmp_path: Path) -> None:
    env = _idle_env(tmp_path)
    session = "fake-claude-session"
    identity = ("--harness", "claude", "--session", session)

    config = _json_success(tmp_path, env, "idle", "config", "--interactive")
    assert isinstance(config, dict)
    assert config["enabled"] is True

    armed = _json_success(
        tmp_path,
        env,
        "idle",
        "arm",
        *identity,
        "--ttl",
        "3600",
    )
    assert isinstance(armed, dict)
    assert armed["stretch"] == 1
    assert armed["anchor"] == 1
    assert all(isinstance(armed[field], int) for field in ("push_at", "warn_at", "compact_at"))

    status = _json_success(tmp_path, env, "idle", "status", "--json")
    assert isinstance(status, list)
    assert status[0]["schedule"] == {
        "push_at": armed["push_at"],
        "warn_at": armed["warn_at"],
        "compact_at": armed["compact_at"],
    }
    assert status[0]["done"] == {}

    pushed = _json_success(
        tmp_path,
        env,
        "idle",
        "fire",
        "push",
        *identity,
        "--stretch",
        "1",
        "--anchor",
        "1",
    )
    assert pushed == {"decision": "act", "reason": "guards-passed"}
    status = _json_success(tmp_path, env, "idle", "status", "--json")
    assert isinstance(status, list)
    assert status[0]["done"] == {"push": "sent"}

    compacted = _json_success(
        tmp_path,
        env,
        "idle",
        "fire",
        "compact",
        *identity,
        "--stretch",
        "1",
        "--anchor",
        "1",
        "--draft",
        "no",
        "--context-tokens",
        "120000",
    )
    assert compacted == {"decision": "act", "reason": "guards-passed"}
    status = _json_success(tmp_path, env, "idle", "status", "--json")
    assert isinstance(status, list)
    assert status[0]["done"] == {"compact": "claimed", "push": "sent"}

    done = _json_success(
        tmp_path,
        env,
        "idle",
        "done",
        "compact",
        *identity,
        "--stretch",
        "1",
        "--result",
        "ok",
    )
    assert done == {}
    status = _json_success(tmp_path, env, "idle", "status", "--json")
    assert isinstance(status, list)
    assert status[0]["done"] == {"compact": "ok", "push": "sent"}

    returned = _json_success(
        tmp_path,
        env,
        "idle",
        "return",
        *identity,
        "--user-prompt",
    )
    assert returned == {"stretch_closed": True}
    assert _json_success(tmp_path, env, "idle", "status", "--json") == []


@pytest.mark.integration
def test_idle_role_gate_and_json_error_contract(tmp_path: Path) -> None:
    env = _idle_env(tmp_path, role="spawn")

    config = _json_success(tmp_path, env, "idle", "config", "--interactive")
    assert isinstance(config, dict)
    assert config["enabled"] is False
    assert config["reason"] == "role"

    armed = _json_success(
        tmp_path,
        env,
        "idle",
        "arm",
        "--harness",
        "claude",
        "--session",
        "spawn-session",
        "--ttl",
        "3600",
    )
    assert armed == {"anchor": None, "stretch": None}
    idle_root = Path(env["MERIDIAN_HOME"]) / "idle"
    assert not tuple(idle_root.glob("*.json"))

    env["MERIDIAN_SESSION_ROLE"] = "primary"
    bad_stage = _run(
        tmp_path,
        env,
        "idle",
        "fire",
        "invalid",
        "--harness",
        "claude",
        "--session",
        "fake-session",
        "--stretch",
        "1",
        "--anchor",
        "1",
    )
    assert bad_stage.returncode == 1
    assert json.loads(bad_stage.stdout) == {"error": "unknown idle stage: invalid"}
    assert bad_stage.stderr == ""

    missing_session = _run(tmp_path, env, "idle", "arm", "--harness", "claude")
    assert missing_session.returncode == 1
    assert set(json.loads(missing_session.stdout)) == {"error"}
    assert missing_session.stderr == ""

    missing_harness_value = _run(tmp_path, env, "idle", "arm", "--harness")
    assert missing_harness_value.returncode == 1
    assert json.loads(missing_harness_value.stdout) == {"error": "--harness requires a value"}
    assert missing_harness_value.stderr == ""


@pytest.mark.integration
def test_idle_outside_meridian_requires_interactive_assertion(tmp_path: Path) -> None:
    env = _idle_env(tmp_path, role=None)

    disabled = _json_success(tmp_path, env, "idle", "config")
    enabled = _json_success(tmp_path, env, "idle", "config", "--interactive")

    assert isinstance(disabled, dict)
    assert disabled["enabled"] is False
    assert disabled["reason"] == "interactive"
    assert isinstance(enabled, dict)
    assert enabled["enabled"] is True
    assert "reason" not in enabled


@pytest.mark.integration
def test_idle_hidden_root_help_but_group_help_and_mod_path_work(tmp_path: Path) -> None:
    env = _idle_env(tmp_path)

    root_help = _run(tmp_path, env, "--help", "--mode", "human")
    assert root_help.returncode == 0
    assert "\n  idle " not in root_help.stdout

    idle_help = _run(tmp_path, env, "idle", "--help", "--mode", "human")
    assert idle_help.returncode == 0
    assert "Usage: meridian idle COMMAND" in idle_help.stdout
    assert "mod-path" in idle_help.stdout

    mod_path = _run(tmp_path, env, "idle", "mod-path")
    assert mod_path.returncode == 0
    assert mod_path.stderr == ""
    assert (Path(mod_path.stdout.strip()) / ".claude-plugin" / "plugin.json").is_file()
