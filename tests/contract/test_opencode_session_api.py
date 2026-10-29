"""OpenCode session API request-shape and retry contract coverage."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import cast

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections import opencode_http
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.harness.connections.opencode_http import OpenCodeConnection
from meridian.lib.harness.projections.project_opencode_streaming import (
    HarnessCapabilityMismatch,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from tests.support.async_determinism import AsyncDeterminism
from tests.support.launch_paths import launch_paths


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
        launch=launch_paths(tmp_path, SpawnId("p-open-1")),
        harness_id=HarnessId.OPENCODE,
        prompt="hello",
        control_root=tmp_path,
        child_env={},
    )

    session_id = await connection._create_session(
        ResolvedLaunchSpec(
            prompt="hello",
            model="openai/spec-model",
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
    )

    with pytest.raises(HarnessCapabilityMismatch, match="continue_fork"):
        await connection._create_session(spec)
    assert connection.requests == []


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
    )

    assert await connection._create_session(spec) == "sess-parent"
    assert connection.requests == [("/session/sess-parent", {})]


@pytest.mark.asyncio
async def test_create_session_uses_native_nested_model_and_never_drops_it():
    spec = ResolvedLaunchSpec(
        prompt="hello",
        model="openai/gpt-test",
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


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_launch_config_binds_compiled_permissions_to_reserved_agent(version: str) -> None:
    import json
    from pathlib import Path

    from meridian.lib.harness.opencode_backend import OpenCodeVersion
    from meridian.lib.harness.projections.permissions_opencode import (
        compile_opencode_permissions,
    )
    from meridian.lib.harness.projections.project_opencode_config import (
        project_opencode_launch_config,
    )
    from meridian.lib.safety.policy import PermissionPolicy, PlatformCommand, PlatformRule
    from meridian.lib.tools import ToolRule

    config = {
        "theme": "native",
        "agent": {"build": {"model": "old/model", "temperature": 0.2}},
        "agents": {"build": {"model": "new/model", "temperature": 0.4}},
        "instructions": ["/user/instructions.md"],
    }
    permissions, report = compile_opencode_permissions(
        ResolvedLaunchSpec(
            opencode_version=cast("OpenCodeVersion", version),
            permission_policy=PermissionPolicy(
                rules=(
                    ToolRule(capability="*", action="deny"),
                    ToolRule(
                        capability="bash",
                        pattern="git *",
                        action="allow",
                        native=("bash(git *)",),
                    ),
                    ToolRule(
                        capability="bash",
                        pattern="git push *",
                        action="deny",
                        native=("bash(git push *)",),
                    ),
                    ToolRule(
                        capability="bash",
                        pattern="meridian spawn *",
                        action="deny",
                        native=("bash(meridian spawn *)",),
                    ),
                    ToolRule(
                        capability="bash",
                        pattern="meridian spawn rearm",
                        action="ask",
                        native=("bash(meridian spawn rearm)",),
                    ),
                    ToolRule(
                        capability="write",
                        pattern="/tmp/**",
                        action="allow",
                        native=("write(/tmp/**)",),
                    ),
                    ToolRule(capability="edit", action="deny", native=("edit",)),
                ),
                platform_rules=(
                    PlatformRule(
                        kind="meridian-commands",
                        commands=(
                            PlatformCommand(
                                command="meridian spawn done",
                                match="prefix",
                                role="completion",
                            ),
                            PlatformCommand(
                                command="meridian spawn rearm",
                                match="prefix",
                                role="completion",
                            ),
                            PlatformCommand(
                                command="meridian task-dir",
                                match="exact",
                                role="read",
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
    projected = json.loads(
        project_opencode_launch_config(
            json.dumps(config),
            version=cast("OpenCodeVersion", version),
            roots=(Path("/repo"),),
            agent_id="meridian-coder",
            prompt_file=Path("/spawns/p1/system-prompt.md"),
            permissions=permissions,
            model="openai/selected",
        )
    )
    agents_key = "agent" if version == "v1" else "agents"
    permission_key = "permission" if version == "v1" else "permissions"
    prompt_key = "prompt" if version == "v1" else "system"
    agent = projected[agents_key]["meridian-coder"]
    assert projected["theme"] == "native"
    assert projected["model"] == "openai/selected"
    assert projected["default_agent"] == "meridian-coder"
    assert projected["instructions"] == ["/user/instructions.md"]
    assert projected["agent"]["build"] == {"model": "old/model", "temperature": 0.2}
    assert projected["agents"]["build"] == {"model": "new/model", "temperature": 0.4}
    assert agent["mode"] == "primary"
    assert agent[prompt_key] == "{file:/spawns/p1/system-prompt.md}"
    if version == "v1":
        assert agent[permission_key] == {
            "*": "deny",
            "bash": {
                "git *": "allow",
                "git push *": "deny",
                "meridian spawn *": "deny",
                "meridian spawn done": "allow",
                "meridian spawn done *": "allow",
                "meridian spawn rearm": "allow",
                "meridian spawn rearm *": "allow",
                "meridian task-dir": "allow",
            },
            "external_directory": {"/repo/**": "allow"},
            "write": {"/tmp/**": "allow"},
            "edit": "deny",
        }
    else:
        assert agent[permission_key] == [
            {"action": "*", "resource": "*", "effect": "deny"},
            {"action": "edit", "resource": "*", "effect": "deny"},
            {"action": "shell", "resource": "git *", "effect": "allow"},
            {"action": "shell", "resource": "git push *", "effect": "deny"},
            {
                "action": "shell",
                "resource": "meridian spawn *",
                "effect": "deny",
            },
            {
                "action": "shell",
                "resource": "meridian task-dir",
                "effect": "allow",
            },
            {
                "action": "shell",
                "resource": "meridian spawn done",
                "effect": "allow",
            },
            {
                "action": "shell",
                "resource": "meridian spawn done *",
                "effect": "allow",
            },
            {
                "action": "shell",
                "resource": "meridian spawn rearm",
                "effect": "allow",
            },
            {
                "action": "shell",
                "resource": "meridian spawn rearm *",
                "effect": "allow",
            },
            {"action": "external_directory", "resource": "/repo/**", "effect": "allow"},
        ]
        write_line = next(
            line for line in report if line.element == "tools: write(/tmp/**) = allow"
        )
        assert write_line.status == "partial"
        assert (
            "conflicting semantics conservatively use permission edit(*): deny" in write_line.note
        )
    completion_line = next(
        line for line in report if line.element.startswith("platform: allow Meridian commands")
    )
    assert completion_line.status == "platform"
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
@pytest.mark.parametrize(
    ("decision", "expected_response", "status", "session_id"),
    (
        ("accept", "once", 204, "ses_1"),
        ("once", "once", 204, "ses_1"),
        ("always", "always", 204, "ses_1"),
        ("reject", "reject", 204, "ses_1"),
        ("unrecognized", "once", 204, "ses_1"),
        # No connection-level session: the pending request's own session is used, and 200
        # is an accepted reply status.
        ("reject", "reject", 200, None),
    ),
)
async def test_respond_request_v1_posts_to_permissions_endpoint(
    decision: str,
    expected_response: str,
    status: int,
    session_id: str | None,
) -> None:
    connection = _TestableOpenCodeConnection([(status, None, "")])
    if session_id is not None:
        connection._session_id = session_id
    connection._pending_requests["per_1"] = "ses_1"

    await connection.respond_request("per_1", decision)

    assert connection.requests == [
        ("/session/ses_1/permissions/per_1", {"response": expected_response}),
    ]
    assert connection._pending_requests == {}


@pytest.mark.asyncio
async def test_respond_request_v1_raises_without_a_session() -> None:
    connection = _TestableOpenCodeConnection([])

    with pytest.raises(ValueError, match="No pending OpenCode permission request"):
        await connection.respond_request("per_missing", "reject")
