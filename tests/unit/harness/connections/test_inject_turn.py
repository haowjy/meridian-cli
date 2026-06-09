"""Connection-level turn injection seam tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    ConnectionNotReady,
    ConnectionState,
    HarnessConnection,
    HarnessEvent,
    StopProgressCallback,
    StopResult,
)
from meridian.lib.harness.connections.codex_ws import CodexConnection
from meridian.lib.harness.connections.opencode_http import OpenCodeConnection
from meridian.lib.launch.launch_types import ResolvedLaunchSpec


class _MinimalConnection(HarnessConnection[ResolvedLaunchSpec]):
    @property
    def state(self) -> ConnectionState:
        return "created"

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.CLAUDE

    @property
    def spawn_id(self) -> SpawnId:
        return SpawnId("p-inject-base")

    @property
    def capabilities(self) -> ConnectionCapabilities:
        return ConnectionCapabilities(
            mid_turn_injection="queue",
            supports_steer=False,
            supports_cancel=False,
            runtime_model_switch=False,
            structured_reasoning=False,
        )

    @property
    def session_id(self) -> str | None:
        return None

    @property
    def subprocess_pid(self) -> int | None:
        return None

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = config, spec

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = reason, progress
        return StopResult()

    def health(self) -> bool:
        return True

    async def send_user_message(self, text: str) -> None:
        _ = text

    async def send_cancel(self) -> None:
        return None

    async def events(self) -> AsyncIterator[HarnessEvent]:
        if False:
            yield HarnessEvent(event_type="never", payload={}, harness_id="claude")


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


def test_base_connection_has_no_resident_backend_by_default() -> None:
    assert _MinimalConnection().resident_backend is None


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

