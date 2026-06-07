# qa-validated: test-suite-redesign
"""Tests for OpenCodeConnection lifecycle — events, start, message,
launch process, type contracts."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import get_args, get_origin

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections import opencode_http
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    ConnectionState,
    HarnessConnection,
    HarnessEvent,
    StopProgressCallback,
    StopResult,
)
from meridian.lib.harness.connections.opencode_http import (
    OpenCodeConnection,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import (
    UnsafeNoOpPermissionResolver,
)

OPENCODE_ACTIVITY_IDLE_EVENT = "session.idle"
OPENCODE_ACTIVITY_ERROR_EVENT = "session.error"


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


class _FakeSseContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def read(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    async def iter_chunked(self, _size: int):  # type: ignore[no-untyped-def]
        for chunk in self._chunks:
            yield chunk


class _BlockingSseContent:
    async def read(self, _size: int) -> bytes:
        await asyncio.Event().wait()
        return b""


class _FakeSseResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self.content = _FakeSseContent(chunks)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _BlockingSseResponse:
    def __init__(self) -> None:
        self.content = _BlockingSseContent()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _LivenessProbeOpenCodeConnection(OpenCodeConnection):
    def __init__(self, responses: list[_FakeSseResponse | _BlockingSseResponse]) -> None:
        super().__init__()
        self._state = "connected"
        self._session_id = "sess-liveness"
        self._process = _FakeProcess()
        self._responses = iter(responses)
        self.open_count = 0

    async def _open_event_stream(self) -> _FakeSseResponse | _BlockingSseResponse:
        self.open_count += 1
        try:
            return next(self._responses)
        except StopIteration as exc:
            raise AssertionError("Unexpected _open_event_stream call in test") from exc


async def _no_sleep(_delay: float) -> None:
    return None


def _build_connection_config(tmp_path: Path) -> ConnectionConfig:
    return ConnectionConfig(
        spawn_id=SpawnId("p-open-observer"),
        harness_id=HarnessId.OPENCODE,
        prompt="hello from test",
        control_root=tmp_path,
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
async def test_opencode_events_fail_after_liveness_timeout_without_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _LivenessProbeOpenCodeConnection(
        responses=[_FakeSseResponse([]) for _ in range(100)]
    )

    monkeypatch.setattr(OpenCodeConnection, "_LIVENESS_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(OpenCodeConnection, "_EVENT_RETRY_DELAY_SECONDS", 0.001)

    events = [event async for event in connection.events()]

    assert events == []
    assert connection.open_count > 1
    assert connection.state == "failed"


@pytest.mark.asyncio
async def test_opencode_events_fail_when_open_stream_stays_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _LivenessProbeOpenCodeConnection(responses=[_BlockingSseResponse()])

    monkeypatch.setattr(OpenCodeConnection, "_LIVENESS_TIMEOUT_SECONDS", 0.01)

    events = [event async for event in connection.events()]

    assert events == []
    assert connection.open_count == 1
    assert connection.state == "failed"


@pytest.mark.asyncio
async def test_opencode_events_refresh_liveness_deadline_after_yielded_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _LivenessProbeOpenCodeConnection(
        responses=[
            _FakeSseResponse([b'{"type":"session.idle","sessionID":"sess-liveness"}\n']),
            _FakeSseResponse([]),
        ]
    )
    times = iter([0.0, 0.0, 0.1, 0.7, 0.8, 0.8, 1.0, 1.3])

    monkeypatch.setattr(OpenCodeConnection, "_LIVENESS_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(OpenCodeConnection, "_EVENT_RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(opencode_http.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(opencode_http.time, "monotonic", lambda: next(times, 1.3))

    events = [event async for event in connection.events()]

    assert [event.event_type for event in events] == ["session.idle"]
    assert connection.open_count == 2
    assert connection.state == "failed"


def test_opencode_health_fails_when_event_liveness_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = OpenCodeConnection()
    connection._state = "connected"
    connection._process = _FakeProcess()
    connection._last_health_ok = True
    connection._last_event_time = 10.0

    monkeypatch.setattr(OpenCodeConnection, "_LIVENESS_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(opencode_http.time, "monotonic", lambda: 10.5)
    assert connection.health() is True

    monkeypatch.setattr(opencode_http.time, "monotonic", lambda: 11.1)
    assert connection.health() is False


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
    assert captured["cwd"] == str(config.control_root)
    env_result = captured["env"]
    assert isinstance(env_result, dict)
    assert env_result["MERIDIAN_INHERIT_CALLED"] == "1"
    assert env_result["MERIDIAN_TEST_ENV"] == "1"
    assert "OPENCODE_CONFIG_CONTENT" in env_result
    import json as _json

    oc_config = _json.loads(env_result["OPENCODE_CONFIG_CONTENT"])
    assert len(oc_config["instructions"]) == 1
    instruction_path = Path(oc_config["instructions"][0])
    assert instruction_path.parent == Path(tempfile.gettempdir())
    assert instruction_path.name.startswith("meridian-sysprompt-")
    assert instruction_path.suffix == ".md"
    assert connection.subprocess_pid == fake_process.pid

    await connection._cleanup_runtime()


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

        async def events(self):  # type: ignore[no-untyped-def]
            if False:
                yield HarnessEvent(
                    event_type="noop",
                    payload={},
                    harness_id=HarnessId.OPENCODE.value,
                )

    with pytest.raises(TypeError):
        _MissingCancel()
