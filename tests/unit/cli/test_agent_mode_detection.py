"""Agent-mode detection keys on managed-session membership, not delegation depth.

The primary launched harness (the root agent) runs at ``MERIDIAN_DEPTH=0`` yet is
still an agent, so depth-based detection wrongly classified it as a human. The
discriminator is the injected ``MERIDIAN_SPAWN_ID`` (present for the primary and
every subagent; absent in a human's raw shell).
"""

from __future__ import annotations

import pytest

from meridian.cli.startup.help import detect_agent_mode
from meridian.lib.core.depth import is_managed_meridian_session


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, False),  # human raw shell
        ({"MERIDIAN_SPAWN_ID": ""}, False),  # empty value is not a session
        ({"MERIDIAN_SPAWN_ID": "   "}, False),  # whitespace-only is not a session
        ({"MERIDIAN_SPAWN_ID": "p4393", "MERIDIAN_DEPTH": "0"}, True),  # primary root agent
        ({"MERIDIAN_SPAWN_ID": "p5001", "MERIDIAN_DEPTH": "2"}, True),  # nested subagent
        # MANAGED alone is set by the entrypoint setdefault even for a human shell:
        ({"MERIDIAN_MANAGED": "1", "MERIDIAN_DEPTH": "0"}, False),
    ],
)
def test_managed_session_uses_spawn_id_not_depth(env: dict[str, str], expected: bool) -> None:
    assert is_managed_meridian_session(env) is expected


def test_detect_agent_mode_primary_session_is_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: depth-0 primary agent (has SPAWN_ID) renders agent help."""

    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p4393")
    monkeypatch.setenv("MERIDIAN_DEPTH", "0")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    assert detect_agent_mode() is True


def test_detect_agent_mode_human_shell_is_human(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    assert detect_agent_mode() is False


def test_detect_agent_mode_interactive_terminal_downshifts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A human attaching an interactive terminal inside a managed session gets human help."""

    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p4393")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    assert detect_agent_mode() is False


def test_detect_agent_mode_flags_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)
    assert detect_agent_mode(force_agent=True) is True

    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p4393")
    assert detect_agent_mode(force_human=True) is False
