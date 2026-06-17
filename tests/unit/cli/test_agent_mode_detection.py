"""Agent-mode detection keys on managed-session membership, not delegation depth."""

from __future__ import annotations

import sys

import pytest

from meridian.cli.mode import is_agent_render_mode, resolve_render_mode
from meridian.lib.core.depth import is_managed_meridian_session


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, False),
        ({"MERIDIAN_SPAWN_ID": ""}, False),
        ({"MERIDIAN_SPAWN_ID": "   "}, False),
        ({"MERIDIAN_SPAWN_ID": "p4393", "MERIDIAN_DEPTH": "0"}, True),
        ({"MERIDIAN_SPAWN_ID": "p5001", "MERIDIAN_DEPTH": "2"}, True),
        ({"MERIDIAN_MANAGED": "1", "MERIDIAN_DEPTH": "0"}, False),
    ],
)
def test_managed_session_uses_spawn_id_not_depth(env: dict[str, str], expected: bool) -> None:
    assert is_managed_meridian_session(env) is expected


def _detect_agent_mode(*, forced: str | None = None) -> bool:
    return is_agent_render_mode(
        resolve_render_mode(
            forced=forced,
            stdin_isatty=sys.stdin.isatty(),
            stdout_isatty=sys.stdout.isatty(),
        )
    )


def test_resolve_render_mode_primary_session_is_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p4393")
    monkeypatch.setenv("MERIDIAN_DEPTH", "0")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    assert (
        resolve_render_mode(
            forced=None,
            stdin_isatty=False,
            stdout_isatty=False,
        )
        == "agent"
    )


def test_resolve_render_mode_human_shell_is_human(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    assert _detect_agent_mode() is False


def test_resolve_render_mode_interactive_terminal_downshifts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p4393")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    assert _detect_agent_mode() is False


def test_resolve_render_mode_forced_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)
    assert _detect_agent_mode(forced="agent") is True

    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p4393")
    assert _detect_agent_mode(forced="human") is False


def test_parse_render_mode_rejects_invalid_value() -> None:
    from meridian.cli.mode import parse_render_mode

    with pytest.raises(SystemExit, match="--mode must be one of"):
        parse_render_mode("robot")
