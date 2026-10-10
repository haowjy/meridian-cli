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
        MERIDIAN_IDLE_PUSH_SECONDS="0",
        MERIDIAN_IDLE_COMPACT_MINUTES="1",
        _MERIDIAN_HARNESS="claude",
    )
    if role is not None:
        env["MERIDIAN_SESSION_ROLE"] = role
    return env


def _run(tmp_path: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "meridian", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _json_success(tmp_path: Path, env: dict[str, str], *args: str) -> object:
    result = _run(tmp_path, env, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""
    return json.loads(result.stdout)


@pytest.mark.integration
def test_idle_cli_wire_contract(tmp_path: Path) -> None:
    env = _idle_env(tmp_path)
    identity = ("--harness", "claude", "--session", "fake-claude-session")

    assert _json_success(tmp_path, env, "idle", "config", "--interactive") == {
        "compact": True,
        "compact_minutes": 1,
        "enabled": True,
        "min_compact_tokens": 40_000,
        "push_seconds": 0,
        "warn_minutes": 15,
    }
    armed = _json_success(tmp_path, env, "idle", "arm", *identity, "--ttl", "61")
    assert isinstance(armed, dict)
    assert set(armed) == {"stretch", "anchor", "push_at", "compact_at"}
    assert (armed["stretch"], armed["anchor"]) == (1, 1)
    assert isinstance(armed["push_at"], int)
    assert isinstance(armed["compact_at"], int)

    status = _json_success(tmp_path, env, "idle", "status", "--json")
    assert isinstance(status, list) and len(status) == 1
    updated_at_ms = status[0]["updated_at_ms"]
    assert isinstance(updated_at_ms, int)
    assert status[0] == {
        "v": 1,
        "harness": "claude",
        "session": "fake-claude-session",
        "stretch": 1,
        "stretch_open": True,
        "last_turn_id": None,
        "last_input_count": None,
        "anchor": 1,
        "idle_since_ms": armed["push_at"],
        "ttl_seconds": 61,
        "schedule": {
            "push_at": armed["push_at"],
            "warn_at": None,
            "compact_at": armed["compact_at"],
        },
        "done": {},
        "compact_window_until_ms": None,
        "expect_compaction_turn": False,
        "updated_at_ms": updated_at_ms,
    }
    human = _run(tmp_path, env, "idle", "status")
    assert human.returncode == 0
    assert human.stderr == ""
    assert human.stdout == (
        "HARNESS    SESSION                  STRETCH ANCHOR SCHEDULE\n"
        f"claude     fake-claude-session            1      1 push={armed['push_at']}, "
        f"compact={armed['compact_at']}\n"
    )

    assert _json_success(
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
    ) == {"decision": "act", "reason": "guards-passed"}
    assert (
        _json_success(
            tmp_path,
            env,
            "idle",
            "done",
            "compact",
            *identity,
            "--stretch",
            "1",
            "--result",
            "vetoed",
        )
        == {}
    )
    assert _json_success(tmp_path, env, "idle", "return", *identity, "--user-prompt") == {
        "stretch_closed": True,
        "was_open": True,
    }


@pytest.mark.integration
def test_codex_idle_event_chains_user_notify_after_state_update(tmp_path: Path) -> None:
    env = _idle_env(tmp_path)
    thread_id = "01a11e8c-4466-7771-8f11-14286723b1bb"
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    observed_state = tmp_path / "observed-state.json"
    state_path = Path(env["MERIDIAN_HOME"]) / "idle" / f"codex-{thread_id}.json"
    handler = tmp_path / "notify.sh"
    handler.write_text('#!/bin/sh\ncat "$2" > "$1"\n', encoding="utf-8")
    handler.chmod(0o700)
    notify = ["/bin/sh", handler.as_posix(), observed_state.as_posix(), state_path.as_posix()]
    (codex_home / "config.toml").write_text(
        f"notify = {json.dumps(notify)}\n",
        encoding="utf-8",
    )
    env["CODEX_HOME"] = codex_home.as_posix()
    _json_success(
        tmp_path,
        env,
        "idle",
        "arm",
        "--harness",
        "codex",
        "--session",
        thread_id,
        "--ttl",
        "240",
    )
    payload = json.dumps(
        {
            "type": "agent-turn-complete",
            "thread-id": thread_id,
            "turn-id": "turn-1",
            "input-messages": ["hello"],
        },
        separators=(",", ":"),
    )

    assert _json_success(tmp_path, env, "idle", "event", "--harness", "codex", payload) == {}
    chained_state = json.loads(observed_state.read_text(encoding="utf-8"))
    assert chained_state["last_input_count"] == 1
    assert chained_state["stretch"] == 2
    assert state_path.is_file()


@pytest.mark.integration
def test_idle_cli_error_envelopes_and_role_reasons(tmp_path: Path) -> None:
    env = _idle_env(tmp_path, role=None)
    config = _json_success(tmp_path, env, "idle", "config")
    assert isinstance(config, dict)
    assert (config["enabled"], config["reason"]) == (False, "interactive")
    env["MERIDIAN_SESSION_ROLE"] = "spawn"
    config = _json_success(tmp_path, env, "idle", "config", "--interactive")
    assert isinstance(config, dict)
    assert (config["enabled"], config["reason"]) == (False, "role")

    env["MERIDIAN_SESSION_ROLE"] = "primary"
    invalid = _run(
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
    assert (invalid.returncode, invalid.stderr) == (1, "")
    assert json.loads(invalid.stdout) == {"error": "unknown idle stage: invalid"}

    missing = _run(tmp_path, env, "idle", "arm", "--harness", "claude")
    assert (missing.returncode, missing.stderr) == (1, "")
    assert set(json.loads(missing.stdout)) == {"error"}

    missing_value = _run(tmp_path, env, "idle", "arm", "--harness")
    assert (missing_value.returncode, missing_value.stderr) == (1, "")
    assert json.loads(missing_value.stdout) == {"error": "--harness requires a value"}


@pytest.mark.integration
def test_idle_mod_path_wire_contract(tmp_path: Path) -> None:
    result = _run(tmp_path, _idle_env(tmp_path), "idle", "mod-path")
    assert (result.returncode, result.stderr) == (0, "")
    assert (Path(result.stdout.strip()) / ".claude-plugin" / "plugin.json").is_file()
