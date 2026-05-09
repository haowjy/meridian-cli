from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import get_args, get_origin

import pytest

from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.harness.adapter import SpawnParams
from meridian.lib.harness.connections import opencode_http
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    ConnectionState,
    HarnessConnection,
    HarnessEvent,
)
from meridian.lib.harness.connections.opencode_http import (
    OpenCodeConnection,
    SessionNotReadyError,
    _materialize_system_prompt,
)
from meridian.lib.harness.opencode import OpenCodeAdapter
from meridian.lib.harness.projections.project_opencode_streaming import (
    HarnessCapabilityMismatch,
    project_opencode_spec_to_serve_command,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import (
    PermissionConfig,
    TieredPermissionResolver,
    UnsafeNoOpPermissionResolver,
)

OPENCODE_ACTIVITY_IDLE_EVENT = "session.idle"
OPENCODE_ACTIVITY_ERROR_EVENT = "session.error"


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




class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 9001
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _StartProbeOpenCodeConnection(OpenCodeConnection):
    def __init__(self) -> None:
        super().__init__()
        self.initial_messages: list[tuple[str, str | None]] = []
        self.launch_calls = 0
        self.create_session_calls = 0

    async def _launch_process(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = config, spec
        self.launch_calls += 1
        self._process = _FakeProcess()

    async def _create_session_with_retry(
        self,
        spec: ResolvedLaunchSpec,
        *,
        timeout_seconds: float,
    ) -> str:
        _ = spec, timeout_seconds
        self.create_session_calls += 1
        return "sess-primary-observer"

    async def _post_session_message(self, text: str, *, system: str | None = None) -> None:
        self.initial_messages.append((text, system))


def _build_connection_config(tmp_path: Path) -> ConnectionConfig:
    return ConnectionConfig(
        spawn_id=SpawnId("p-open-observer"),
        harness_id=HarnessId.OPENCODE,
        prompt="hello from test",
        project_root=tmp_path,
        env_overrides={"MERIDIAN_TEST_ENV": "1"},
        system="system from test",
    )


def test_opencode_activity_event_names_are_pinned() -> None:
    assert OPENCODE_ACTIVITY_IDLE_EVENT == "session.idle"
    assert OPENCODE_ACTIVITY_ERROR_EVENT == "session.error"


@pytest.mark.parametrize(
    "event_type",
    (OPENCODE_ACTIVITY_IDLE_EVENT, OPENCODE_ACTIVITY_ERROR_EVENT),
)
def test_opencode_event_from_json_line_pins_activity_transition_events(event_type: str) -> None:
    connection = OpenCodeConnection()
    connection._signal_in_flight = True

    event = connection._event_from_json_line(
        json_text=f'{{"type":"{event_type}","sessionID":"sess-activity"}}',
        raw_text=f'{{"type":"{event_type}","sessionID":"sess-activity"}}',
    )

    assert event is not None
    assert event.event_type == event_type
    assert event.payload == {"type": event_type, "sessionID": "sess-activity"}
    assert connection._signal_in_flight is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("use_start_observer", "expected_initial_messages"),
    ((False, [("hello from test", "system from test")]), (True, [])),
)
async def test_opencode_start_primary_observer_mode_controls_initial_prompt_post(
    tmp_path: Path,
    use_start_observer: bool,
    expected_initial_messages: list[tuple[str, str | None]],
) -> None:
    connection = _StartProbeOpenCodeConnection()
    config = _build_connection_config(tmp_path)
    spec = ResolvedLaunchSpec(
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    if use_start_observer:
        await connection.start_observer(config, spec)
    else:
        await connection.start(config, spec)

    assert connection.state == "connected"
    assert connection.session_id == "sess-primary-observer"
    assert connection.launch_calls == 1
    assert connection.create_session_calls == 1
    assert connection.initial_messages == expected_initial_messages

    await connection.stop()


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
async def test_opencode_launch_process_passes_env_overrides_to_inherit_child_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection = OpenCodeConnection()
    config = _build_connection_config(tmp_path)
    spec = ResolvedLaunchSpec(
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )
    fake_process = _FakeProcess()
    captured: dict[str, object] = {}

    def _fake_inherit_child_env(
        _base_env: Mapping[str, str],
        overrides: dict[str, str],
    ) -> dict[str, str]:
        captured["overrides"] = dict(overrides)
        return {"MERIDIAN_INHERIT_CALLED": "1", **overrides}

    async def _fake_create_subprocess_exec(
        *command: str,
        cwd: str,
        env: Mapping[str, str],
        stdout: object,
        stderr: object,
        **_kwargs: object,
    ) -> _FakeProcess:
        _ = stdout, stderr
        captured["command"] = list(command)
        captured["cwd"] = cwd
        captured["env"] = dict(env)
        return fake_process

    monkeypatch.setattr(opencode_http, "_find_free_port", lambda: 17777)
    monkeypatch.setattr(opencode_http, "inherit_child_env", _fake_inherit_child_env)
    monkeypatch.setattr(
        opencode_http,
        "project_opencode_spec_to_serve_command",
        lambda _spec, host, port: ["opencode", "serve", "--host", host, "--port", str(port)],
    )
    monkeypatch.setattr(
        opencode_http.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    await connection._launch_process(config, spec)

    assert captured["overrides"] == config.env_overrides
    assert captured["cwd"] == str(config.project_root)
    # _materialize_system_prompt injects OPENCODE_CONFIG_CONTENT after inherit
    env_result = captured["env"]
    assert isinstance(env_result, dict)
    assert env_result["MERIDIAN_INHERIT_CALLED"] == "1"
    assert env_result["MERIDIAN_TEST_ENV"] == "1"
    # System prompt from config.system materialised as instruction file
    assert "OPENCODE_CONFIG_CONTENT" in env_result
    import json as _json

    oc_config = _json.loads(env_result["OPENCODE_CONFIG_CONTENT"])
    assert len(oc_config["instructions"]) == 1
    assert oc_config["instructions"][0].startswith("/tmp/meridian-sysprompt-")
    assert oc_config["instructions"][0].endswith(".md")
    assert connection.subprocess_pid == fake_process.pid

    await connection._cleanup_runtime()


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
async def test_create_session_with_retry_resume_retries_get_transport_error_then_succeeds() -> None:
    connection = _TestableOpenCodeConnection(
        responses=[],
        get_responses=[
            ConnectionResetError("response body truncated"),
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


@pytest.mark.asyncio
async def test_create_session_with_retry_continue_fork_fails_immediately() -> None:
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
        await connection._create_session_with_retry(spec, timeout_seconds=1.0)

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


def test_opencode_streaming_serve_command_keeps_verbatim_extra_args_tail() -> None:
    command = project_opencode_spec_to_serve_command(
        ResolvedLaunchSpec(
            prompt="hello",
            extra_args=("--port", "9999"),
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
        host="127.0.0.1",
        port=4096,
    )

    assert command[-2:] == ["--port", "9999"]


def test_opencode_connection_inherits_harness_connection_base() -> None:
    assert issubclass(OpenCodeConnection, HarnessConnection)
    assert HarnessConnection in OpenCodeConnection.__mro__


def test_opencode_connection_explicitly_binds_harness_connection_generic() -> None:
    matching_bases = [
        base
        for base in getattr(OpenCodeConnection, "__orig_bases__", ())
        if get_origin(base) is HarnessConnection
    ]

    assert matching_bases
    assert get_args(matching_bases[0]) == (ResolvedLaunchSpec,)


def test_missing_harness_connection_abstract_method_raises_type_error() -> None:
    class _MissingCancel(HarnessConnection[ResolvedLaunchSpec]):
        @property
        def state(self) -> ConnectionState:
            return "created"

        @property
        def harness_id(self) -> HarnessId:
            return HarnessId.OPENCODE

        @property
        def spawn_id(self) -> SpawnId:
            return SpawnId("missing-cancel")

        @property
        def capabilities(self) -> ConnectionCapabilities:
            return ConnectionCapabilities(
                mid_turn_injection="http_post",
                supports_steer=False,
                supports_cancel=True,
                runtime_model_switch=False,
                structured_reasoning=True,
            )

        @property
        def session_id(self) -> str | None:
            return None

        @property
        def subprocess_pid(self) -> int | None:
            return None

        async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
            _ = config, spec

        async def stop(self) -> None:
            return None

        def health(self) -> bool:
            return True

        async def send_user_message(self, text: str) -> None:
            _ = text

        async def events(self):  # type: ignore[no-untyped-def]
            if False:
                yield HarnessEvent(
                    event_type="noop",
                    payload={},
                    harness_id=HarnessId.OPENCODE.value,
                )

    with pytest.raises(TypeError):
        _MissingCancel()


# -- _materialize_system_prompt tests ------------------------------------------


def test_materialize_system_prompt_writes_temp_file_and_injects_instruction(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "p-test"
    spawn_dir.mkdir(parents=True)
    env: dict[str, str] = {}

    _materialize_system_prompt(spawn_dir, "You are an agent.", env)

    import json

    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    instructions = config["instructions"]
    assert len(instructions) == 1
    # File is a temp file, not in spawn_dir
    prompt_path = Path(instructions[0])
    assert prompt_path.exists()
    assert prompt_path.read_text(encoding="utf-8") == "You are an agent."
    assert "meridian-sysprompt-" in prompt_path.name
    prompt_path.unlink()


def test_materialize_system_prompt_merges_with_existing_config_content(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "p-merge"
    spawn_dir.mkdir(parents=True)
    import json

    existing = json.dumps({"permission": {"external_directory": {"/some/path": "allow"}}})
    env: dict[str, str] = {"OPENCODE_CONFIG_CONTENT": existing}

    _materialize_system_prompt(spawn_dir, "system text", env)

    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    # Existing permission config preserved
    assert config["permission"] == {"external_directory": {"/some/path": "allow"}}
    # Instructions added as temp file
    assert len(config["instructions"]) == 1
    prompt_path = Path(config["instructions"][0])
    assert prompt_path.exists()
    prompt_path.unlink()


def test_materialize_system_prompt_merges_with_existing_instructions(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "p-existing-instructions"
    spawn_dir.mkdir(parents=True)
    import json

    existing = json.dumps({"instructions": ["/existing/agents.md"]})
    env: dict[str, str] = {"OPENCODE_CONFIG_CONTENT": existing}

    _materialize_system_prompt(spawn_dir, "more instructions", env)

    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert config["instructions"][0] == "/existing/agents.md"
    assert len(config["instructions"]) == 2
    prompt_path = Path(config["instructions"][1])
    assert prompt_path.exists()
    prompt_path.unlink()


def test_materialize_system_prompt_noop_when_system_is_none(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "p-none"
    spawn_dir.mkdir(parents=True)
    env: dict[str, str] = {}

    _materialize_system_prompt(spawn_dir, None, env)

    assert "OPENCODE_CONFIG_CONTENT" not in env


def test_materialize_system_prompt_noop_when_system_is_whitespace(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "p-whitespace"
    spawn_dir.mkdir(parents=True)
    env: dict[str, str] = {}

    _materialize_system_prompt(spawn_dir, "   \n  ", env)

    assert "OPENCODE_CONFIG_CONTENT" not in env
