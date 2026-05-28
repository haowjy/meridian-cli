"""Tests for canonical Pi path resolution."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.harness.pi_paths import (
    pi_agent_dir_env_override,
    pi_meridian_state_dir_env_override,
    resolve_meridian_pi_extension_root,
    resolve_meridian_pi_state_dir,
    resolve_pi_agent_dir,
    resolve_pi_default_user_extension_dir,
    resolve_pi_extension_target_root,
    resolve_pi_spawn_session_root,
)
from meridian.lib.state.user_paths import get_user_home


def test_resolve_pi_agent_dir_defaults_to_home_pi_agent() -> None:
    assert resolve_pi_agent_dir() == Path.home() / ".pi" / "agent"


def test_resolve_pi_agent_dir_honors_env_override() -> None:
    assert resolve_pi_agent_dir(env={"PI_CODING_AGENT_DIR": "/custom/agent"}) == Path(
        "/custom/agent"
    )


def test_resolve_pi_default_user_extension_dir_under_agent() -> None:
    assert resolve_pi_default_user_extension_dir() == resolve_pi_agent_dir() / "extensions"


def test_meridian_pi_extension_root_defaults_under_meridian_home() -> None:
    assert resolve_meridian_pi_extension_root() == get_user_home() / "pi" / "extensions"


def test_meridian_pi_state_dir_prefers_explicit_env() -> None:
    assert resolve_meridian_pi_state_dir(
        env={"MERIDIAN_PI_STATE_DIR": "/state/root"}
    ) == Path("/state/root")


def test_meridian_pi_state_dir_defaults_without_session_dir() -> None:
    assert resolve_meridian_pi_state_dir(
        env={"PI_CODING_AGENT_SESSION_DIR": "/sessions/spawn-1"}
    ) == get_user_home() / "meridian-pi" / "state"


def test_pi_meridian_state_dir_env_override_uses_explicit_runtime_root() -> None:
    runtime_root = Path("/resolved/runtime")
    assert pi_meridian_state_dir_env_override(runtime_root=runtime_root) == {
        "MERIDIAN_PI_STATE_DIR": "/resolved/runtime"
    }


def test_pi_meridian_state_dir_env_override_without_runtime_root_uses_default_state() -> None:
    env = {"PI_CODING_AGENT_SESSION_DIR": "/scoped/session"}
    expected = str(get_user_home() / "meridian-pi" / "state")
    assert pi_meridian_state_dir_env_override(env=env) == {
        "MERIDIAN_PI_STATE_DIR": expected
    }


def test_extension_target_root_lives_under_agent_dir_legacy() -> None:
    root = resolve_pi_extension_target_root("abc123")
    assert root == resolve_pi_agent_dir() / "extensions" / "meridian" / "abc123"


def test_pi_agent_dir_env_override_matches_resolve() -> None:
    assert pi_agent_dir_env_override()["PI_CODING_AGENT_DIR"] == str(resolve_pi_agent_dir())


def test_spawn_session_root_is_meridian_scoped() -> None:
    path = resolve_pi_spawn_session_root()
    assert path.name == "sessions"
    assert path.parent.name == "meridian-pi"
