"""Tests for canonical Pi path resolution."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.harness.pi_paths import (
    pi_agent_dir_env_override,
    resolve_pi_agent_dir,
    resolve_pi_extension_target_root,
    resolve_pi_spawn_session_root,
)


def test_resolve_pi_agent_dir_defaults_to_home_pi_agent() -> None:
    assert resolve_pi_agent_dir() == Path.home() / ".pi" / "agent"


def test_resolve_pi_agent_dir_honors_env_override() -> None:
    assert resolve_pi_agent_dir(env={"PI_CODING_AGENT_DIR": "/custom/agent"}) == Path(
        "/custom/agent"
    )


def test_extension_target_root_lives_under_agent_dir() -> None:
    root = resolve_pi_extension_target_root("abc123")
    assert root == resolve_pi_agent_dir() / "extensions" / "meridian" / "abc123"


def test_pi_agent_dir_env_override_matches_resolve() -> None:
    assert pi_agent_dir_env_override()["PI_CODING_AGENT_DIR"] == str(resolve_pi_agent_dir())


def test_spawn_session_root_is_meridian_scoped() -> None:
    path = resolve_pi_spawn_session_root()
    assert path.name == "sessions"
    assert path.parent.name == "meridian-pi"
