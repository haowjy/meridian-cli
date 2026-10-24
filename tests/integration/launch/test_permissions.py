
import pytest

from meridian.lib.core.types import ModelId
from meridian.lib.harness.adapter import SpawnParams
from meridian.lib.harness.claude import ClaudeAdapter
from meridian.lib.harness.pi import PiAdapter
from meridian.lib.launch.env import (
    apply_pi_bind_time_env,
    build_harness_child_env,
    inherit_child_env,
    merge_env_overrides,
)
from meridian.lib.safety.policy import EffectiveNetworkFacts, build_permission_policy

_MERIDIAN_RUNTIME_KEYS = (
    "MERIDIAN_PROJECT_DIR",
    "_MERIDIAN_DEPTH",
    "MERIDIAN_CHAT_ID",
    "MERIDIAN_CONTEXT_KB_DIR",
    "MERIDIAN_ACTIVE_WORK_ID",
    "MERIDIAN_ACTIVE_WORK_DIR",
    "MERIDIAN_CONTEXT_WORK_DIR",
    "MERIDIAN_CONTEXT_WORK_ARCHIVE_DIR",
)


def test_invalid_approval_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported approval mode"):
        build_permission_policy(
            network_facts=EffectiveNetworkFacts.from_values(
                sandbox="danger-full-access", network=None
            ),
            approval="sometimes",
        )


def test_build_harness_child_env_inherits_allowed_vars_and_blocks_adapter_vars() -> None:
    child_env = build_harness_child_env(
        base_env={
            "PATH": "/usr/bin",
            "UNRELATED_TOKEN": "keep-me",
            "MISC_VALUE": "keep-too",
            "CLAUDECODE": "1",
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "67",
            "MERIDIAN_SECRET_DATABASE": "drop-me",
            "MERIDIAN_PRIMARY_PROMPT": "stale",
        },
        adapter=ClaudeAdapter(),
        run_params=SpawnParams(prompt="test", model=ModelId("claude-sonnet-4-6")),
        runtime_env_overrides={"_MERIDIAN_DEPTH": "2"},
    )

    assert child_env["PATH"] == "/usr/bin"
    assert child_env["UNRELATED_TOKEN"] == "keep-me"
    assert child_env["MISC_VALUE"] == "keep-too"
    assert child_env["_MERIDIAN_DEPTH"] == "2"
    assert child_env["MERIDIAN_PRIMARY_PROMPT"] == "stale"
    assert child_env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "67"
    assert "CLAUDECODE" not in child_env
    assert "MERIDIAN_SECRET_DATABASE" not in child_env


def test_inherit_child_env_blocks_meridian_runtime_dir() -> None:
    inherited = inherit_child_env(
        base_env={
            "PATH": "/usr/bin",
            "_MERIDIAN_RUNTIME_DIR": "/stale/runtime",
        },
        env_overrides={
            "MERIDIAN_PROJECT_DIR": "/repo",
            "_MERIDIAN_PI_STATE_DIR": "/resolved/runtime",
        },
    )

    assert "_MERIDIAN_RUNTIME_DIR" not in inherited
    assert inherited["MERIDIAN_PROJECT_DIR"] == "/repo"
    assert inherited["_MERIDIAN_PI_STATE_DIR"] == "/resolved/runtime"


def test_build_harness_child_env_pi_state_dir_from_runtime_root() -> None:
    runtime_root = "/resolved/runtime"
    child_env = build_harness_child_env(
        base_env={
            "PATH": "/usr/bin",
            "_MERIDIAN_PI_STATE_DIR": "/stale/pi-state",
            "_MERIDIAN_RUNTIME_DIR": "/stale/runtime",
        },
        adapter=PiAdapter(),
        run_params=SpawnParams(prompt="test", model=ModelId("openai-codex/gpt-5.4-mini")),
        runtime_env_overrides={
            "_MERIDIAN_PI_STATE_DIR": runtime_root,
            "MERIDIAN_PROJECT_DIR": "/repo",
        },
    )

    assert child_env["_MERIDIAN_PI_STATE_DIR"] == runtime_root
    assert child_env["MERIDIAN_PROJECT_DIR"] == "/repo"
    assert "_MERIDIAN_RUNTIME_DIR" not in child_env
    assert "PI_CODING_AGENT_SESSION_DIR" in child_env


def test_pi_directory_defaults_preserve_explicit_launch_environment() -> None:
    env = build_harness_child_env(
        base_env={
            "PI_CODING_AGENT_DIR": "/requested/auth",
            "PI_CODING_AGENT_SESSION_DIR": "/requested/sessions",
        },
        adapter=PiAdapter(),
        run_params=SpawnParams(prompt="test"),
    )
    assert env["PI_CODING_AGENT_DIR"] == "/requested/auth"
    assert env["PI_CODING_AGENT_SESSION_DIR"] == "/requested/sessions"
    env = build_harness_child_env(
        base_env={"PI_CODING_AGENT_DIR": "/inherited/auth"},
        adapter=PiAdapter(),
        run_params=SpawnParams(prompt="test"),
        runtime_env_overrides={
            "PI_CODING_AGENT_DIR": "/launch/auth",
            "PI_CODING_AGENT_SESSION_DIR": "/launch/sessions",
        },
    )
    assert env["PI_CODING_AGENT_DIR"] == "/launch/auth"
    assert env["PI_CODING_AGENT_SESSION_DIR"] == "/launch/sessions"


def test_pi_bind_time_env_contains_agent_dir_timeout_and_task_ping() -> None:
    child_env = build_harness_child_env(
        base_env={"PATH": "/usr/bin", "HOME": "/home/tester"},
        adapter=PiAdapter(),
        run_params=SpawnParams(prompt="test", model=ModelId("openai-codex/gpt-5.4-mini")),
    )

    apply_pi_bind_time_env(
        child_env,
        launch_role="spawned",
        timeout_seconds=2.75,
        interval_seconds=0.125,
        reset_on_activity=False,
    )

    assert child_env["PI_CODING_AGENT_SESSION_DIR"]
    assert child_env["_MERIDIAN_PI_CHILD_WAVE_TIMEOUT_MS"] == "2750"
    assert child_env["_MERIDIAN_PI_TASK_PING_INTERVAL_MS"] == "125"
    assert child_env["_MERIDIAN_PI_TASK_PING_RESET_ON_ACTIVITY"] == "false"


def test_inherit_child_env_runtime_overrides_win() -> None:
    inherited = inherit_child_env(
        base_env={
            "PATH": "/usr/bin",
            "FOO": "base",
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "67",
        },
        env_overrides={
            "FOO": "override",
            "_MERIDIAN_DEPTH": "2",
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "42",
        },
    )

    assert inherited["PATH"] == "/usr/bin"
    assert inherited["FOO"] == "override"
    assert inherited["_MERIDIAN_DEPTH"] == "2"
    assert inherited["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "42"


def test_merge_env_overrides_rejects_meridian_keys_from_plan() -> None:
    for key in _MERIDIAN_RUNTIME_KEYS:
        with pytest.raises(RuntimeError) as exc_info:
            merge_env_overrides(
                plan_overrides={key: "blocked"},
                runtime_overrides={"_MERIDIAN_DEPTH": "2"},
            )
        assert f"{key} via plan_overrides" in str(exc_info.value)


def test_merge_env_overrides_accepts_runtime_meridian_keys() -> None:
    runtime_overrides = {
        "MERIDIAN_PROJECT_DIR": "/repo",
        "_MERIDIAN_DEPTH": "2",
        "MERIDIAN_CHAT_ID": "c-parent",
        "MERIDIAN_CONTEXT_KB_DIR": "/repo/.meridian/kb",
        "MERIDIAN_ACTIVE_WORK_ID": "current",
        "MERIDIAN_ACTIVE_WORK_DIR": "/repo/.meridian/work/current",
        "MERIDIAN_CONTEXT_WORK_DIR": "/repo/.meridian/work",
        "MERIDIAN_CONTEXT_WORK_ARCHIVE_DIR": "/repo/.meridian/archive/work",
    }

    merged = merge_env_overrides(
        plan_overrides={},
        runtime_overrides=runtime_overrides,
    )

    assert merged == runtime_overrides


def test_merge_env_overrides_non_meridian_precedence() -> None:
    merged = merge_env_overrides(
        plan_overrides={
            "FOO": "plan",
            "PLAN_ONLY": "plan",
            "EMPTY_VALUE": "",
            "MULTILINE_VALUE": "line-1\nline-2",
        },
        runtime_overrides={
            "FOO": "runtime",
            "_MERIDIAN_DEPTH": "2",
            "RUNTIME_ONLY": "runtime",
        },
    )

    assert merged == {
        "FOO": "runtime",
        "PLAN_ONLY": "plan",
        "RUNTIME_ONLY": "runtime",
        "_MERIDIAN_DEPTH": "2",
        "EMPTY_VALUE": "",
        "MULTILINE_VALUE": "line-1\nline-2",
    }
