import json

import pytest

from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.harness.adapter import SpawnParams
from meridian.lib.harness.claude import ClaudeAdapter
from meridian.lib.harness.pi import PiAdapter
from meridian.lib.harness.projections.permission_flags import resolve_permission_flags
from meridian.lib.launch.env import (
    apply_pi_bind_time_env,
    build_harness_child_env,
    inherit_child_env,
    merge_env_overrides,
    sanitize_child_env,
)
from meridian.lib.safety.permissions import (
    PermissionConfig,
    ToolsPermissionResolver,
    build_permission_config,
    compile_tools_to_opencode_permission,
    resolve_permission_pipeline,
)

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
        build_permission_config("danger-full-access", approval="sometimes")


def test_opencode_tools_map_normalizes_and_sets_explicit_override() -> None:
    normalized = json.loads(
        compile_tools_to_opencode_permission(
            {
                "*": "deny",
                "read": "allow",
                "glob": "allow",
                "grep": "allow",
                "bash(git status)": "allow",
                "web": "allow",
            }
        )
    )
    assert normalized == {
        "*": "deny",
        "read": "allow",
        "glob": "allow",
        "grep": "allow",
        "bash(git status)": "allow",
        "webfetch": "allow",
        "websearch": "allow",
    }

    config, resolver = resolve_permission_pipeline(
        sandbox="workspace-write",
        tools={"*": "deny", "read": "allow", "write": "allow"},
        approval="confirm",
    )
    assert isinstance(resolver, ToolsPermissionResolver)
    assert config.sandbox == "workspace-write"
    assert json.loads(config.opencode_permission_override or "{}") == {
        "*": "deny",
        "read": "allow",
        "write": "allow",
    }


def test_opencode_deny_defaults_and_claude_tool_projection() -> None:
    normalized = json.loads(
        compile_tools_to_opencode_permission(
            {
                "*": "allow",
                "read": "deny",
                "glob": "deny",
                "grep": "deny",
                "bash(git status)": "deny",
                "web": "deny",
            }
        )
    )
    assert normalized == {
        "*": "allow",
        "read": "deny",
        "glob": "deny",
        "grep": "deny",
        "bash(git status)": "deny",
        "webfetch": "deny",
        "websearch": "deny",
    }

    deny_only_config, deny_only_resolver = resolve_permission_pipeline(
        sandbox="workspace-write",
        tools={"*": "allow", "read": "deny", "write": "deny"},
        approval="confirm",
    )
    assert isinstance(deny_only_resolver, ToolsPermissionResolver)
    assert json.loads(deny_only_config.opencode_permission_override or "{}") == {
        "*": "allow",
        "read": "deny",
        "write": "deny",
    }

    combined_config, combined_resolver = resolve_permission_pipeline(
        sandbox="workspace-write",
        tools={"*": "deny", "bash": "allow", "agent": "deny"},
    )
    assert isinstance(combined_resolver, ToolsPermissionResolver)
    assert resolve_permission_flags(combined_resolver, HarnessId.CLAUDE) == (
        "--allowedTools",
        "Bash",
    )
    assert json.loads(combined_config.opencode_permission_override or "{}") == {
        "*": "deny",
        "bash": "allow",
        "agent": "deny",
    }


def test_claude_tool_projection_keeps_allow_rules_without_deny_default() -> None:
    _config, resolver = resolve_permission_pipeline(
        sandbox="danger-full-access",
        tools={
            "bash": "allow",
            "bash(meridian spawn *)": "allow",
            "bash(meridian session *)": "allow",
            "agent": "deny",
            "write": "deny",
        },
        approval="auto",
    )

    assert isinstance(resolver, ToolsPermissionResolver)
    assert resolve_permission_flags(resolver, HarnessId.CLAUDE) == (
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        "Bash,Bash(meridian spawn *),Bash(meridian session *)",
        "--disallowedTools",
        "Agent,Write",
    )


def test_tools_resolver_codex_ignores_claude_tool_flags() -> None:
    config, resolver = resolve_permission_pipeline(
        sandbox="workspace-write",
        tools={"*": "allow", "bash": "deny"},
    )

    assert config.sandbox == "workspace-write"
    assert isinstance(resolver, ToolsPermissionResolver)
    assert resolve_permission_flags(resolver, HarnessId.CODEX) == (
        "--sandbox",
        "workspace-write",
    )


@pytest.mark.parametrize(
    ("sandbox", "expected_sandbox", "expected_flags"),
    (
        ("default", "default", ()),
        ("read-only", "read-only", ("--sandbox", "read-only")),
    ),
)
def test_codex_uses_exact_sandbox_from_profile(
    sandbox: str,
    expected_sandbox: str,
    expected_flags: tuple[str, ...],
) -> None:
    config, resolver = resolve_permission_pipeline(sandbox=sandbox)

    assert config.sandbox == expected_sandbox
    assert resolve_permission_flags(resolver, HarnessId.CODEX) == expected_flags


def test_sanitize_child_env_strips_secrets() -> None:
    sanitized = sanitize_child_env(
        base_env={
            "PATH": "/usr/bin",
            "HOME": "/home/tester",
            "LC_ALL": "C.UTF-8",
            "XDG_RUNTIME_DIR": "/tmp/xdg",
            "UV_CACHE_DIR": "/tmp/uv",
            "EXAMPLE_TOKEN": "drop-me",
            "EXAMPLE_KEY": "drop-me-too",
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "67",
            "ANTHROPIC_API_KEY": "allowed-credential",
        },
        env_overrides={
            "_MERIDIAN_DEPTH": "2",
            "CUSTOM_SECRET": "explicit-override",
        },
        pass_through={"ANTHROPIC_API_KEY"},
    )

    assert sanitized["PATH"] == "/usr/bin"
    assert sanitized["HOME"] == "/home/tester"
    assert sanitized["LC_ALL"] == "C.UTF-8"
    assert sanitized["XDG_RUNTIME_DIR"] == "/tmp/xdg"
    assert sanitized["UV_CACHE_DIR"] == "/tmp/uv"
    assert sanitized["ANTHROPIC_API_KEY"] == "allowed-credential"
    assert sanitized["_MERIDIAN_DEPTH"] == "2"
    assert sanitized["CUSTOM_SECRET"] == "explicit-override"
    assert "EXAMPLE_TOKEN" not in sanitized
    assert "EXAMPLE_KEY" not in sanitized
    assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" not in sanitized


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
        permission_config=PermissionConfig(),
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
        permission_config=PermissionConfig(),
        runtime_env_overrides={
            "_MERIDIAN_PI_STATE_DIR": runtime_root,
            "MERIDIAN_PROJECT_DIR": "/repo",
        },
    )

    assert child_env["_MERIDIAN_PI_STATE_DIR"] == runtime_root
    assert child_env["MERIDIAN_PROJECT_DIR"] == "/repo"
    assert "_MERIDIAN_RUNTIME_DIR" not in child_env
    assert "PI_CODING_AGENT_SESSION_DIR" in child_env


def test_pi_bind_time_env_contains_agent_dir_timeout_and_task_ping() -> None:
    child_env = build_harness_child_env(
        base_env={"PATH": "/usr/bin", "HOME": "/home/tester"},
        adapter=PiAdapter(),
        run_params=SpawnParams(prompt="test", model=ModelId("openai-codex/gpt-5.4-mini")),
        permission_config=PermissionConfig(),
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


def test_merge_env_overrides_rejects_meridian_keys_from_plan_and_preflight() -> None:
    for source in ("plan_overrides", "preflight_overrides"):
        for key in _MERIDIAN_RUNTIME_KEYS:
            plan_overrides = {key: "blocked"} if source == "plan_overrides" else {}
            preflight_overrides = {key: "blocked"} if source == "preflight_overrides" else {}
            with pytest.raises(RuntimeError) as exc_info:
                merge_env_overrides(
                    plan_overrides=plan_overrides,
                    runtime_overrides={"_MERIDIAN_DEPTH": "2"},
                    preflight_overrides=preflight_overrides,
                )
            assert f"{key} via {source}" in str(exc_info.value)


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
        preflight_overrides={},
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
        preflight_overrides={
            "FOO": "preflight",
            "PREFLIGHT_ONLY": "preflight",
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
        "PREFLIGHT_ONLY": "preflight",
        "RUNTIME_ONLY": "runtime",
        "_MERIDIAN_DEPTH": "2",
        "EMPTY_VALUE": "",
        "MULTILINE_VALUE": "line-1\nline-2",
    }
