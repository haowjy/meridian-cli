"""Connection-level turn injection seam tests."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from meridian.lib.harness.connections.base import ConnectionNotReady
from meridian.lib.harness.connections.codex_ws import CodexConnection
from meridian.lib.harness.connections.opencode_http import OpenCodeConnection


class _RequestCaptureCodexConnection(CodexConnection):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[str, dict[str, object] | None]] = []

    async def _request(
        self,
        method: str,
        params: dict[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        _ = timeout_seconds
        self.requests.append((method, params))
        return {}


class _PostCaptureOpenCodeConnection(OpenCodeConnection):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[str, dict[str, object]]] = []

    async def _post_json(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        skip_body_on_statuses: frozenset[int] | None = None,
        tolerate_incomplete_body: bool = False,
    ) -> tuple[int, object | None, str]:
        _ = skip_body_on_statuses, tolerate_incomplete_body
        self.requests.append((path, dict(payload)))
        return 202, None, "application/json"


@pytest.mark.asyncio
async def test_codex_resident_backend_starts_new_turn_on_current_thread() -> None:
    connection = _RequestCaptureCodexConnection()
    connection._state = "connected"
    connection._thread_id = "thread-123"

    await connection.resident_backend.begin_followup_turn("next task")

    assert connection.requests == [
        (
            "turn/start",
            {
                "threadId": "thread-123",
                "input": [{"type": "text", "text": "next task"}],
            },
        )
    ]


@pytest.mark.asyncio
async def test_codex_resident_backend_requires_idle_backend() -> None:
    connection = _RequestCaptureCodexConnection()
    connection._state = "connected"
    connection._thread_id = "thread-123"
    connection._current_turn_id = "turn-active"

    with pytest.raises(ConnectionNotReady, match="require an idle backend"):
        await connection.resident_backend.begin_followup_turn("too soon")

    assert connection.requests == []


@pytest.mark.asyncio
async def test_codex_resident_backend_requires_current_thread() -> None:
    connection = _RequestCaptureCodexConnection()
    connection._state = "connected"

    with pytest.raises(ConnectionNotReady, match="thread ID is unavailable"):
        await connection.resident_backend.begin_followup_turn("no session")

    assert connection.requests == []


@pytest.mark.asyncio
async def test_opencode_resident_backend_posts_prompt_to_current_session() -> None:
    connection = _PostCaptureOpenCodeConnection()
    connection._state = "connected"
    connection._base_url = "http://127.0.0.1:9999"
    connection._session_id = "sess-123"

    await connection.resident_backend.begin_followup_turn("next task")

    assert connection.requests == [
        (
            "/session/sess-123/prompt_async",
            {"parts": [{"type": "text", "text": "next task"}]},
        )
    ]


@pytest.mark.asyncio
async def test_opencode_resident_backend_requires_current_session() -> None:
    connection = _PostCaptureOpenCodeConnection()
    connection._state = "connected"

    with pytest.raises(ConnectionNotReady, match="session has not been created"):
        await connection.resident_backend.begin_followup_turn("no session")

    assert connection.requests == []
