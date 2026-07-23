"""OpenCode session API request-shape and retry integration coverage."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections import opencode_http
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.harness.connections.opencode_http import OpenCodeConnection
from meridian.lib.harness.projections.project_opencode_streaming import (
    HarnessCapabilityMismatch,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import (
    UnsafeNoOpPermissionResolver,
)
from tests.support.async_determinism import AsyncDeterminism


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


class _PayloadTimeoutOpenCodeConnection(OpenCodeConnection):
    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[dict[str, object]] = []

    async def _post_json(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        skip_body_on_statuses: frozenset[int] | None = None,
        tolerate_incomplete_body: bool = False,
    ) -> tuple[int, object | None, str]:
        _ = path, skip_body_on_statuses, tolerate_incomplete_body
        payload_dict = dict(payload)
        self.payloads.append(payload_dict)
        if payload_dict:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        return 200, {"id": "sess-empty-fallback"}, "application/json"


@pytest.mark.asyncio
async def test_create_session_uses_spec_model_not_connection_config(tmp_path) -> None:  # type: ignore[no-untyped-def]
    connection = _TestableOpenCodeConnection(responses=[(200, {"session_id": "sess-1"}, "")])
    connection._config = ConnectionConfig(
        spawn_id=SpawnId("p-open-1"),
        harness_id=HarnessId.OPENCODE,
        prompt="hello",
        control_root=tmp_path,
        child_env={},
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


@pytest.mark.asyncio
async def test_post_session_message_includes_system_field_when_present() -> None:
    connection = _TestableOpenCodeConnection(responses=[(204, None, "")])
    connection._session_id = "sess-system"

    await connection._post_session_message("user turn", system="system prompt")

    assert connection.requests == [
        (
            "/session/sess-system/prompt_async",
            {
                "parts": [{"type": "text", "text": "user turn"}],
                "system": "system prompt",
            },
        )
    ]


@pytest.mark.asyncio
async def test_session_creation_falls_back_when_projected_payload_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    determinism = AsyncDeterminism(start=0.0)
    determinism.install(monkeypatch, monotonic_modules=(opencode_http,))
    determinism.install_on_running_loop(monkeypatch)
    connection = _PayloadTimeoutOpenCodeConnection()
    monkeypatch.setattr(OpenCodeConnection, "_SESSION_CREATE_PAYLOAD_TIMEOUT_SECONDS", 0.01)

    create_task = asyncio.create_task(
        connection._create_session_with_retry(
            ResolvedLaunchSpec(
                model="gpt-5.5",
                agent_name="prober",
                permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
            ),
            timeout_seconds=1.0,
        )
    )
    while not create_task.done():
        await determinism.sleep(0.01)

    assert await create_task == "sess-empty-fallback"
    assert connection.payloads == [
        {"model": "gpt-5.5", "modelID": "gpt-5.5", "agent": "prober"},
        {},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_response",
    [
        (404, None, ""),
        ConnectionRefusedError("server not listening yet"),
    ],
    ids=["404", "transport-error"],
)
async def test_create_session_with_retry_fresh_retries_then_succeeds(
    first_response: tuple[int, object | None, str] | ConnectionRefusedError,
) -> None:
    connection = _TestableOpenCodeConnection(
        responses=[
            first_response,
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
    assert len(connection.requests) == 2
    assert all(path == "/session" for path, _payload in connection.requests)


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
    assert len(connection.requests) == 3


