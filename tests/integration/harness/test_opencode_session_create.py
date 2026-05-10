# qa-validated: test-suite-redesign
"""Tests for OpenCodeConnection._create_session — model, skills, agent, fork, resume."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.harness.adapter import SpawnParams
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.harness.connections.opencode_http import (
    OpenCodeConnection,
    SessionNotReadyError,
)
from meridian.lib.harness.opencode import OpenCodeAdapter
from meridian.lib.harness.projections.project_opencode_streaming import (
    HarnessCapabilityMismatch,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import (
    PermissionConfig,
    TieredPermissionResolver,
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
        project_root=tmp_path,
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
async def test_create_session_uses_already_normalized_model_from_launch_spec() -> None:
    resolver = TieredPermissionResolver(config=PermissionConfig())
    run = SpawnParams(prompt="hello", model=ModelId("gpt-5.3-codex"))
    spec = OpenCodeAdapter().resolve_launch_spec(run, resolver)

    connection = _TestableOpenCodeConnection(responses=[(200, {"session_id": "sess-2"}, "")])
    await connection._create_session(spec)

    assert isinstance(spec, ResolvedLaunchSpec)
    assert connection.requests[0][1]["model"] == "gpt-5.3-codex"
    assert connection.requests[0][1]["modelID"] == "gpt-5.3-codex"


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
async def test_create_session_omits_skills_when_default_prompt_inline_policy_is_used() -> None:
    resolver = TieredPermissionResolver(config=PermissionConfig())
    run = SpawnParams(
        prompt="hello",
        model=ModelId("gpt-5.3-codex"),
        skills=("skill-a", "skill-b"),
    )
    spec = OpenCodeAdapter().resolve_launch_spec(run, resolver)

    connection = _TestableOpenCodeConnection(responses=[(200, {"session_id": "sess-inline"}, "")])
    await connection._create_session(spec)

    payload = connection.requests[0][1]
    assert spec.skills == ()
    assert "skills" not in payload


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
async def test_create_session_logs_unsupported_effort(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connection = _TestableOpenCodeConnection(responses=[(200, {"session_id": "sess-4"}, "")])
    spec = ResolvedLaunchSpec(
        prompt="hello",
        model="gpt-5.3-codex",
        effort="high",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    with caplog.at_level(
        logging.DEBUG, logger="meridian.lib.harness.projections.project_opencode_streaming"
    ):
        await connection._create_session(spec)

    payload = connection.requests[0][1]
    assert payload["model"] == "gpt-5.3-codex"
    assert payload["modelID"] == "gpt-5.3-codex"
    assert "does not support effort override" in caplog.text


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
async def test_create_session_resume_verifies_existing_session_via_get() -> None:
    connection = _TestableOpenCodeConnection(
        responses=[],
        get_responses=[(200, {"id": "sess-parent"}, "")],
    )
    spec = ResolvedLaunchSpec(
        prompt="hello",
        model="gpt-5.3-codex",
        continue_session_id="sess-parent",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    session_id = await connection._create_session(spec)
    assert session_id == "sess-parent"
    assert connection.requests == [("/session/sess-parent", {})]


@pytest.mark.asyncio
async def test_create_session_resume_raises_on_get_404() -> None:
    # A 404 means the server has not yet loaded the session; we raise a
    # retryable error so _create_session_with_retry can poll until timeout.
    connection = _TestableOpenCodeConnection(
        responses=[],
        get_responses=[(404, None, "")],
    )
    spec = ResolvedLaunchSpec(
        prompt="hello",
        model="gpt-5.3-codex",
        continue_session_id="sess-parent",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    with pytest.raises(RuntimeError, match="not yet loaded"):
        await connection._create_session(spec)
    assert connection.requests == [("/session/sess-parent", {})]


@pytest.mark.asyncio
async def test_create_session_resume_rejects_fork_even_when_get_succeeds() -> None:
    # continue_fork must be rejected even if the session exists on the server.
    connection = _TestableOpenCodeConnection(
        responses=[],
        get_responses=[(200, {"id": "sess-parent"}, "")],
    )
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
