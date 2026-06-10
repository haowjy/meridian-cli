"""OpenCode session API tests: creation, resume, and retry behavior."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections import opencode_http
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.harness.connections.opencode_http import OpenCodeConnection, SessionNotReadyError
from meridian.lib.harness.projections.project_opencode_streaming import (
    HarnessCapabilityMismatch,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import (
    UnsafeNoOpPermissionResolver,
)


class _TestableOpenCodeConnection(OpenCodeConnection):
    def __init__(
        self,
        responses: list[tuple[int, object | None, str] | Exception],
        *,
        get_responses: list[tuple[int, object | None, str] | Exception] | None = None,
    ) -> None:
        super().__init__()
        self.requests: list[tuple[str, dict[str, object]]] = []
        self._responses = iter(responses)
        self._get_responses = iter(get_responses) if get_responses else iter([])

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
        try:
            response = next(self._responses)
        except StopIteration as exc:
            raise AssertionError("Unexpected _post_json call in test") from exc
        if isinstance(response, Exception):
            raise response
        return response

    async def _get_json(
        self,
        path: str,
    ) -> tuple[int, object | None, str]:
        self.requests.append((path, {}))
        try:
            response = next(self._get_responses)
        except StopIteration as exc:
            raise AssertionError("Unexpected _get_json call in test") from exc
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.asyncio
async def test_create_session_uses_spec_model_not_connection_config(tmp_path) -> None:  # type: ignore[no-untyped-def]
    connection = _TestableOpenCodeConnection(responses=[(200, {"session_id": "sess-1"}, "")])
    connection._config = ConnectionConfig(
        spawn_id=SpawnId("p-open-1"),
        harness_id=HarnessId.OPENCODE,
        prompt="hello",
        control_root=tmp_path,
        env_overrides={},
    )

    session_id = await connection._create_session(
        ResolvedLaunchSpec(
            prompt="hello",
            model="spec-model",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        )
    )

    assert session_id == "sess-1"
    assert connection.requests[0][1]["model"] == "spec-model"
    assert connection.requests[0][1]["modelID"] == "spec-model"


@pytest.mark.asyncio
async def test_create_session_omits_model_fields_when_launch_spec_model_is_none() -> None:
    connection = _TestableOpenCodeConnection(responses=[(200, {"session_id": "sess-none"}, "")])

    await connection._create_session(
        ResolvedLaunchSpec(
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        )
    )

    payload = connection.requests[0][1]
    assert "model" not in payload
    assert "modelID" not in payload


@pytest.mark.asyncio
async def test_create_session_forwards_agent_and_skills_from_opencode_launch_spec() -> None:
    connection = _TestableOpenCodeConnection(responses=[(200, {"session_id": "sess-3"}, "")])

    await connection._create_session(
        ResolvedLaunchSpec(
            prompt="hello",
            model="gpt-5.3-codex",
            agent_name="worker",
            skills=("skill-a", "skill-b"),
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        )
    )

    payload = connection.requests[0][1]
    assert payload["agent"] == "worker"
    assert payload["skills"] == ["skill-a", "skill-b"]


@pytest.mark.asyncio
async def test_create_session_raises_when_continue_fork_requested() -> None:
    # continue_fork is rejected before any network I/O.
    connection = _TestableOpenCodeConnection(responses=[])
    spec = ResolvedLaunchSpec(
        prompt="hello",
        model="gpt-5.3-codex",
        continue_session_id="sess-parent",
        continue_fork=True,
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    with pytest.raises(HarnessCapabilityMismatch, match="continue_fork"):
        await connection._create_session(spec)
    assert connection.requests == []


@pytest.mark.asyncio
async def test_create_session_forwards_mcp_tools_in_payload() -> None:
    connection = _TestableOpenCodeConnection(responses=[(200, {"session_id": "sess-6"}, "")])
    spec = ResolvedLaunchSpec(
        prompt="hello",
        model="openrouter/gpt-4o-mini",
        mcp_tools=("tool-a=echo a", "tool-b=echo b"),
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    await connection._create_session(spec)

    payload = connection.requests[0][1]
    assert payload["mcp"] == {"servers": ["tool-a=echo a", "tool-b=echo b"]}


class _FakeAiohttpResponse:
    def __init__(
        self,
        *,
        status: int,
        headers: Mapping[str, str] | None = None,
        text_result: str | None = None,
        text_error: Exception | None = None,
    ) -> None:
        self.status = status
        self.headers = dict(headers or {"Content-Type": "application/json"})
        self._text_result = text_result
        self._text_error = text_error
        self.text_calls = 0

    async def __aenter__(self) -> _FakeAiohttpResponse:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None

    async def text(self) -> str:
        self.text_calls += 1
        if self._text_error is not None:
            raise self._text_error
        return self._text_result or ""


class _FakeAiohttpClient:
    def __init__(self, responses: list[_FakeAiohttpResponse]) -> None:
        self._responses = iter(responses)
        self.urls: list[str] = []

    def get(self, url: str) -> _FakeAiohttpResponse:
        self.urls.append(url)
        try:
            return next(self._responses)
        except StopIteration as exc:
            raise AssertionError("Unexpected fake aiohttp GET in test") from exc


class _TestableGetJsonOpenCodeConnection(OpenCodeConnection):
    def __init__(self, client: _FakeAiohttpClient) -> None:
        super().__init__()
        self._client = client
        self._base_url = "http://127.0.0.1:17777"


@pytest.mark.asyncio
async def test_create_session_with_retry_fresh_retries_404_then_succeeds() -> None:
    connection = _TestableOpenCodeConnection(
        responses=[
            (404, None, ""),
            (200, {"session_id": "sess-fresh"}, ""),
        ],
    )
    spec = ResolvedLaunchSpec(
        prompt="hello",
        model="gpt-5.3-codex",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    session_id = await connection._create_session_with_retry(spec, timeout_seconds=1.0)

    assert session_id == "sess-fresh"
    assert [path for path, _payload in connection.requests] == ["/session", "/session"]


@pytest.mark.asyncio
async def test_create_session_with_retry_fresh_retries_transport_error_then_succeeds() -> None:
    connection = _TestableOpenCodeConnection(
        responses=[
            ConnectionRefusedError("server not listening yet"),
            (200, {"session_id": "sess-fresh"}, ""),
        ],
    )
    spec = ResolvedLaunchSpec(
        prompt="hello",
        model="gpt-5.3-codex",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    session_id = await connection._create_session_with_retry(spec, timeout_seconds=1.0)

    assert session_id == "sess-fresh"
    assert [path for path, _payload in connection.requests] == ["/session", "/session"]


@pytest.mark.asyncio
async def test_create_session_with_retry_resume_retries_404_then_succeeds() -> None:
    connection = _TestableOpenCodeConnection(
        responses=[],
        get_responses=[
            (404, None, ""),
            (404, None, ""),
            (200, {"id": "sess-parent"}, ""),
        ],
    )
    spec = ResolvedLaunchSpec(
        prompt="hello",
        model="gpt-5.3-codex",
        continue_session_id="sess-parent",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    session_id = await connection._create_session_with_retry(spec, timeout_seconds=1.0)

    assert session_id == "sess-parent"
    assert connection.requests == [
        ("/session/sess-parent", {}),
        ("/session/sess-parent", {}),
        ("/session/sess-parent", {}),
    ]


@pytest.mark.asyncio
async def test_real_get_json_body_read_error_bubbles_and_resume_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = ResolvedLaunchSpec(
        prompt="hello",
        model="gpt-5.3-codex",
        continue_session_id="sess-parent",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )
    body_read_error = ConnectionResetError("response body truncated")

    get_json_response = _FakeAiohttpResponse(status=200, text_error=body_read_error)
    get_json_connection = _TestableGetJsonOpenCodeConnection(
        _FakeAiohttpClient([get_json_response])
    )

    with pytest.raises(ConnectionResetError, match="response body truncated") as get_json_exc:
        await get_json_connection._get_json("/session/sess-parent")

    assert get_json_exc.value is body_read_error
    assert get_json_response.text_calls == 1

    create_session_response = _FakeAiohttpResponse(status=200, text_error=body_read_error)
    create_session_connection = _TestableGetJsonOpenCodeConnection(
        _FakeAiohttpClient([create_session_response])
    )

    with pytest.raises(SessionNotReadyError, match="not reachable yet") as create_session_exc:
        await create_session_connection._create_session(spec)

    assert create_session_exc.value.__cause__ is body_read_error
    assert create_session_response.text_calls == 1

    retry_error_response = _FakeAiohttpResponse(status=200, text_error=body_read_error)
    retry_success_response = _FakeAiohttpResponse(
        status=200,
        text_result='{"id": "sess-parent"}',
    )
    retry_connection = _TestableGetJsonOpenCodeConnection(
        _FakeAiohttpClient([retry_error_response, retry_success_response])
    )

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(opencode_http.asyncio, "sleep", _no_sleep)

    session_id = await retry_connection._create_session_with_retry(spec, timeout_seconds=1.0)

    assert session_id == "sess-parent"
    assert retry_error_response.text_calls == 1
    assert retry_success_response.text_calls == 1
    assert retry_connection._client.urls == [
        "http://127.0.0.1:17777/session/sess-parent",
        "http://127.0.0.1:17777/session/sess-parent",
    ]


@pytest.mark.asyncio
async def test_create_session_with_retry_resume_repeated_404_times_out() -> None:
    connection = _TestableOpenCodeConnection(
        responses=[],
        get_responses=[
            (404, None, ""),
            (404, None, ""),
            (404, None, ""),
        ],
    )
    spec = ResolvedLaunchSpec(
        prompt="hello",
        model="gpt-5.3-codex",
        continue_session_id="sess-parent",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    with pytest.raises(TimeoutError, match="did not become ready"):
        await connection._create_session_with_retry(spec, timeout_seconds=0.01)

    assert connection.requests
    assert all(request == ("/session/sess-parent", {}) for request in connection.requests)


@pytest.mark.asyncio
async def test_create_session_with_retry_resume_500_fails_immediately() -> None:
    connection = _TestableOpenCodeConnection(
        responses=[],
        get_responses=[
            (500, {"error": "boom"}, ""),
            (200, {"id": "sess-parent"}, ""),
        ],
    )
    spec = ResolvedLaunchSpec(
        prompt="hello",
        model="gpt-5.3-codex",
        continue_session_id="sess-parent",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    with pytest.raises(RuntimeError, match="GET failed with status=500"):
        await connection._create_session_with_retry(spec, timeout_seconds=1.0)

    assert connection.requests == [("/session/sess-parent", {})]


@pytest.mark.asyncio
async def test_create_session_with_retry_resume_mismatched_id_fails_immediately() -> None:
    connection = _TestableOpenCodeConnection(
        responses=[],
        get_responses=[
            (200, {"id": "sess-other"}, ""),
            (200, {"id": "sess-parent"}, ""),
        ],
    )
    spec = ResolvedLaunchSpec(
        prompt="hello",
        model="gpt-5.3-codex",
        continue_session_id="sess-parent",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    with pytest.raises(RuntimeError, match="mismatched id"):
        await connection._create_session_with_retry(spec, timeout_seconds=1.0)

    assert connection.requests == [("/session/sess-parent", {})]
