# qa-validated: reaper-escape-fix-test-cleanup
"""Contract tests for session_scope cleanup ordering and exception safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.launch.request import SessionRequest
from meridian.lib.launch.session_scope import session_scope
from meridian.lib.launch.types import PrimarySessionMetadata


def _metadata() -> PrimarySessionMetadata:
    return PrimarySessionMetadata(
        harness="codex",
        model="openai/gpt-5.4-mini",
        agent="tester",
        agent_path="/agents/tester",
        skills=(),
        skill_paths=(),
    )


def _request() -> SessionRequest:
    return SessionRequest()


def test_session_scope_stops_then_reclaims_with_resolved_chat_id(tmp_path: Path) -> None:
    """Normal exit should stop first, then reclaim using the resolved chat id."""
    calls: list[tuple[str, str]] = []

    def fake_start(*_args: object, **_kwargs: object) -> str:
        return "chat-abc"

    def fake_stop(_root: Path, chat_id: str) -> None:
        calls.append(("stop", chat_id))

    def fake_reclaim(_root: Path, chat_id: str) -> object:
        calls.append(("reclaim", chat_id))
        return []

    with session_scope(
        runtime_root=tmp_path,
        metadata=_metadata(),
        request=_request(),
        harness_session_id="h-1",
        _start_session=fake_start,
        _stop_session=fake_stop,
        _reclaim_session_scopes=fake_reclaim,
    ):
        pass

    assert calls == [("stop", "chat-abc"), ("reclaim", "chat-abc")]


@pytest.mark.parametrize(
    ("body_raises", "stop_raises", "expected_error"),
    [
        (True, False, "boom"),
        (False, True, "stop failed"),
    ],
)
def test_session_scope_reclaims_on_exception_paths(
    tmp_path: Path,
    body_raises: bool,
    stop_raises: bool,
    expected_error: str,
) -> None:
    """Reclaim must still run when either the body or stop path fails."""
    calls: list[str] = []

    def fake_start(*_args: object, **_kwargs: object) -> str:
        return "chat-xyz"

    def fake_stop(_root: Path, _chat_id: str) -> None:
        calls.append("stop")
        if stop_raises:
            raise RuntimeError("stop failed")

    def fake_reclaim(_root: Path, _chat_id: str) -> object:
        calls.append("reclaim")
        return []

    with pytest.raises(RuntimeError, match=expected_error), session_scope(
        runtime_root=tmp_path,
        metadata=_metadata(),
        request=_request(),
        harness_session_id="h-2",
        _start_session=fake_start,
        _stop_session=fake_stop,
        _reclaim_session_scopes=fake_reclaim,
    ):
        if body_raises:
            raise RuntimeError("boom")

    assert calls == ["stop", "reclaim"]
