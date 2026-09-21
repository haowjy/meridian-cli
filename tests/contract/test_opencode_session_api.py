"""OpenCode session API request-shape and retry contract coverage."""

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
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


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
            model="openai/spec-model",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        )
    )

    assert session_id == "sess-1"
    assert connection.requests[0][1]["model"] == {"id": "spec-model", "providerID": "openai"}
    assert "modelID" not in connection.requests[0][1]


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
            model="openai/gpt-5.3-codex",
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
        model="openai/gpt-5.3-codex",
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

    await connection._post_session_message("user turn", system="system prompt", fresh=True)

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
async def test_session_creation_does_not_replay_when_projected_payload_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    determinism = AsyncDeterminism(start=0.0)
    determinism.install(monkeypatch, monotonic_modules=(opencode_http,))
    determinism.install_on_running_loop(monkeypatch)
    connection = _PayloadTimeoutOpenCodeConnection()

    create_task = asyncio.create_task(
        connection._create_session_with_retry(
            ResolvedLaunchSpec(
                model="openai/gpt-5.5",
                agent_name="prober",
                permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
            ),
            timeout_seconds=1.0,
        )
    )
    while not create_task.done():
        await determinism.sleep(0.01)

    with pytest.raises(TimeoutError):
        await create_task
    assert connection.payloads == [
        {"model": {"id": "gpt-5.5", "providerID": "openai"}, "agent": "prober"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_response",
    [
        (404, None, ""),
    ],
    ids=["404"],
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
        model="openai/gpt-5.3-codex",
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
        continue_session_id="sess-parent",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    session_id = await connection._create_session_with_retry(spec, timeout_seconds=1.0)

    assert session_id == "sess-parent"
    assert len(connection.requests) == 3


@pytest.mark.asyncio
async def test_v1_resume_with_explicit_model_fails_loudly() -> None:
    connection = _TestableOpenCodeConnection(responses=[])
    spec = ResolvedLaunchSpec(
        prompt="hello",
        model="openai/gpt-5.3-codex",
        continue_session_id="sess-parent",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    with pytest.raises(HarnessCapabilityMismatch, match="cannot switch the model"):
        await connection._create_session(spec)
    assert connection.requests == []


@pytest.mark.asyncio
async def test_v1_resume_without_model_still_verifies_and_resumes() -> None:
    connection = _TestableOpenCodeConnection(
        responses=[],
        get_responses=[(200, {"id": "sess-parent"}, "")],
    )
    spec = ResolvedLaunchSpec(
        prompt="hello",
        continue_session_id="sess-parent",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    assert await connection._create_session(spec) == "sess-parent"
    assert connection.requests == [("/session/sess-parent", {})]


@pytest.mark.asyncio
async def test_create_session_uses_native_nested_model_and_never_drops_it():
    spec = ResolvedLaunchSpec(
        prompt="hello",
        model="openai/gpt-test",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )
    connection = _TestableOpenCodeConnection(responses=[(200, {"id": "native"}, "")])
    assert await connection._create_session(spec) == "native"
    assert connection.requests == [
        ("/session", {"model": {"id": "gpt-test", "providerID": "openai"}})
    ]
    rejected = _TestableOpenCodeConnection(responses=[(400, {"error": "invalid model"}, "")])
    with pytest.raises(RuntimeError, match="rejected"):
        await rejected._create_session(spec)
    assert len(rejected.requests) == 1


@pytest.mark.asyncio
async def test_rejected_or_uncertain_session_creation_never_replays_empty_payload() -> None:
    for response in ((400, {"error": "invalid model"}, ""), ConnectionResetError("lost reply")):
        connection = _TestableOpenCodeConnection(responses=[response])
        with pytest.raises((RuntimeError, ConnectionResetError)):
            await connection._create_session_with_retry(
                ResolvedLaunchSpec(
                    model="openai/selected",
                    permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
                ),
                timeout_seconds=1,
            )
        assert connection.requests == [
            ("/session", {"model": {"id": "selected", "providerID": "openai"}})
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["high", "default"])
async def test_followup_preserves_committed_native_model_agent_and_variant(variant) -> None:
    connection = _TestableOpenCodeConnection(
        responses=[(204, None, "")],
        get_responses=[
            (
                200,
                {
                    "agent": "plan",
                    "model": {"id": "switched", "providerID": "native", "variant": variant},
                },
                "",
            )
        ],
    )
    connection._session_id = "existing"
    await connection._post_session_message("followup", model="openai/old-launch-model")
    assert connection.requests[-1][1] == {
        "parts": [{"type": "text", "text": "followup"}],
        "agent": "plan",
        "model": {"providerID": "native", "modelID": "switched"},
        "variant": variant,
    }


@pytest.mark.asyncio
async def test_selected_model_inspection_identifies_native_agent_conflict() -> None:
    connection = _TestableOpenCodeConnection(
        responses=[],
        get_responses=[
            (200, {"providers": [{"id": "openai", "models": {"selected": {}}}]}, ""),
            (200, {"model": "openai/selected", "default_agent": "build"}, ""),
            (
                200,
                [
                    {
                        "name": "build",
                        "mode": "primary",
                        "model": {"providerID": "other", "modelID": "conflicting"},
                    }
                ],
                "",
            ),
        ],
    )
    assert await connection._inspect_selected_model("openai/selected") == "build"


def test_model_config_override_preserves_unrelated_native_configuration() -> None:
    import json

    from meridian.lib.harness.projections.project_opencode_streaming import (
        project_opencode_model_config,
    )

    config = {
        "theme": "native",
        "agent": {
            "build": {"model": "old/model", "temperature": 0.2},
            "plan": {"model": "other/model"},
        },
    }
    projected = json.loads(
        project_opencode_model_config(json.dumps(config), "openai/selected", agent="build")
    )
    assert projected == {
        "theme": "native",
        "model": "openai/selected",
        "agent": {
            "build": {"model": "openai/selected", "temperature": 0.2},
            "plan": {"model": "other/model"},
        },
    }
    assert config["agent"]["build"]["model"] == "old/model"


@pytest.mark.asyncio
async def test_followup_recovers_last_committed_user_choice_when_session_has_none() -> None:
    connection = _TestableOpenCodeConnection(
        responses=[(204, None, "")],
        get_responses=[
            (200, {"id": "existing"}, ""),
            (
                200,
                [
                    {
                        "info": {
                            "role": "user",
                            "agent": "plan",
                            "model": {
                                "providerID": "native",
                                "modelID": "last-choice",
                                "variant": "high",
                            },
                        }
                    },
                    {"info": {"role": "assistant"}},
                ],
                "",
            ),
        ],
    )
    connection._session_id = "existing"
    await connection._post_session_message("continue", model="openai/old-launch")
    assert connection.requests[-1][1] == {
        "parts": [{"type": "text", "text": "continue"}],
        "agent": "plan",
        "model": {"providerID": "native", "modelID": "last-choice"},
        "variant": "high",
    }


@pytest.mark.asyncio
async def test_private_instructions_are_owned_and_removed_on_stop(tmp_path) -> None:
    import json

    from meridian.lib.launch.workspace_projection import OPENCODE_CONFIG_CONTENT_ENV

    inherited = tmp_path / "user-instructions.md"
    inherited.write_text("user-owned")
    env = {OPENCODE_CONFIG_CONTENT_ENV: json.dumps({"instructions": [str(inherited)]})}
    connection = OpenCodeConnection()
    connection._instruction_path = opencode_http._materialize_system_prompt("private", env)
    owned = connection._instruction_path
    assert owned is not None and owned.read_text() == "private"
    assert json.loads(env[OPENCODE_CONFIG_CONTENT_ENV])["instructions"] == [
        str(inherited),
        str(owned),
    ]
    await connection.stop()
    assert not owned.exists()
    assert inherited.read_text() == "user-owned"
    with pytest.raises(ValueError):
        opencode_http._materialize_system_prompt(None, {OPENCODE_CONFIG_CONTENT_ENV: "{bad"})


@pytest.mark.asyncio
async def test_respond_request_v1_posts_to_permissions_endpoint() -> None:
    connection = _TestableOpenCodeConnection([(200, None, "")])
    connection._pending_requests["per_1"] = "ses_1"

    await connection.respond_request("per_1", "reject")

    assert connection.requests == [
        ("/session/ses_1/permissions/per_1", {"response": "reject"}),
    ]
    assert connection._pending_requests == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected_response"),
    (
        ("accept", "once"),
        ("once", "once"),
        ("always", "always"),
        ("reject", "reject"),
        ("unrecognized", "once"),
    ),
)
async def test_respond_request_v1_maps_decision_to_response(
    decision: str,
    expected_response: str,
) -> None:
    connection = _TestableOpenCodeConnection([(204, None, "")])
    connection._session_id = "ses_1"

    await connection.respond_request("per_1", decision)

    assert connection.requests == [
        ("/session/ses_1/permissions/per_1", {"response": expected_response}),
    ]


@pytest.mark.asyncio
async def test_respond_request_v1_raises_without_a_session() -> None:
    connection = _TestableOpenCodeConnection([])

    with pytest.raises(ValueError, match="No pending OpenCode permission request"):
        await connection.respond_request("per_missing", "reject")

