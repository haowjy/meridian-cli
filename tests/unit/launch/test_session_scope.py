"""Contract tests for session_scope: stop-then-reclaim ordering and exception safety."""

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


def test_stop_called_before_reclaim(tmp_path: Path) -> None:
    """_stop_session must be called before _reclaim_session_scopes."""
    call_order: list[str] = []

    def fake_start(*_args: object, **_kwargs: object) -> str:
        return "chat-abc"

    def fake_stop(_root: Path, _chat_id: str) -> None:
        call_order.append("stop")

    def fake_reclaim(_root: Path, _chat_id: str) -> object:
        call_order.append("reclaim")
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

    assert call_order == ["stop", "reclaim"], f"Expected stop then reclaim, got: {call_order}"


def test_both_called_on_exception(tmp_path: Path) -> None:
    """Both _stop_session and _reclaim_session_scopes are called even when the block raises."""
    call_order: list[str] = []

    def fake_start(*_args: object, **_kwargs: object) -> str:
        return "chat-xyz"

    def fake_stop(_root: Path, _chat_id: str) -> None:
        call_order.append("stop")

    def fake_reclaim(_root: Path, _chat_id: str) -> object:
        call_order.append("reclaim")
        return []

    with pytest.raises(RuntimeError, match="boom"), session_scope(
        runtime_root=tmp_path,
        metadata=_metadata(),
        request=_request(),
        harness_session_id="h-2",
        _start_session=fake_start,
        _stop_session=fake_stop,
        _reclaim_session_scopes=fake_reclaim,
    ):
        raise RuntimeError("boom")

    assert call_order == ["stop", "reclaim"], (
        f"Both must be called even on exception, got: {call_order}"
    )


def test_reclaim_receives_resolved_chat_id(tmp_path: Path) -> None:
    """_reclaim_session_scopes is called with the chat_id returned by _start_session."""
    received_chat_ids: list[str] = []

    def fake_start(*_args: object, **_kwargs: object) -> str:
        return "chat-resolved-123"

    def fake_stop(_root: Path, _chat_id: str) -> None:
        pass

    def fake_reclaim(_root: Path, chat_id: str) -> object:
        received_chat_ids.append(chat_id)
        return []

    with session_scope(
        runtime_root=tmp_path,
        metadata=_metadata(),
        request=_request(),
        harness_session_id="h-3",
        _start_session=fake_start,
        _stop_session=fake_stop,
        _reclaim_session_scopes=fake_reclaim,
    ):
        pass

    assert received_chat_ids == ["chat-resolved-123"], (
        f"Expected resolved chat_id, got: {received_chat_ids}"
    )
