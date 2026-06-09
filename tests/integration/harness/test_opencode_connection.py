# qa-validated: test-suite-redesign
"""Tests for OpenCodeConnection lifecycle, liveness, startup, and launch."""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections import opencode_http
from meridian.lib.harness.connections.base import (
    ConnectionConfig,
    HarnessEvent,
)
from meridian.lib.harness.connections.opencode_http import OpenCodeConnection
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.platform.detached_process import ParentDeathLink
from meridian.lib.platform.process_scope import ProcessScopeSnapshot, ScopedProcessHandle
from meridian.lib.safety.permissions import (
    UnsafeNoOpPermissionResolver,
)
from tests.support.fakes import FakeClock

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
        self.ready_calls = 0
        self.create_session_calls = 0

    async def _launch_process(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = config, spec
        self.launch_calls += 1
        self._process = _FakeProcess()

    async def _wait_for_ready(self, *, timeout_seconds: float) -> None:
        _ = timeout_seconds
        self.ready_calls += 1

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


class _HangingSessionOpenCodeConnection(OpenCodeConnection):
    async def _create_session(self, spec: ResolvedLaunchSpec) -> str:
        _ = spec
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


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
        *,
        timeout: float | None = None,
    ) -> tuple[int, object | None, str]:
        _ = timeout
        self.requests.append((path, {}))
        try:
            response = next(self._get_responses)
        except StopIteration as exc:
            raise AssertionError("Unexpected _get_json call in test") from exc
        if isinstance(response, Exception):
            raise response
        return response


class _ReadinessStartupProbeOpenCodeConnection(_TestableOpenCodeConnection):
    def __init__(
        self,
        get_responses: list[tuple[int, object | None, str] | Exception],
    ) -> None:
        super().__init__(responses=[], get_responses=get_responses)

    async def _launch_process(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = config, spec
        self._process = _FakeProcess()

    async def _wait_for_ready(self, *, timeout_seconds: float) -> None:
        await super()._wait_for_ready(timeout_seconds=timeout_seconds)

    async def _create_session_with_retry(
        self,
        spec: ResolvedLaunchSpec,
        *,
        timeout_seconds: float,
    ) -> str:
        _ = spec, timeout_seconds
        return "sess-after-readiness-timeout"

    async def _post_session_message(self, text: str, *, system: str | None = None) -> None:
        _ = text, system


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


class _KeepaliveSseContent:
    async def read(self, _size: int) -> bytes:
        await asyncio.sleep(0.001)
        return b": keepalive\n\n"


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


class _KeepaliveSseResponse:
    def __init__(self) -> None:
        self.content = _KeepaliveSseContent()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _LivenessProbeOpenCodeConnection(OpenCodeConnection):
    def __init__(
        self,
        responses: list[
            _FakeSseResponse | _BlockingSseResponse | _KeepaliveSseResponse | Exception
        ],
    ) -> None:
        super().__init__()
        self._state = "connected"
        self._session_id = "sess-liveness"
        self._process = _FakeProcess()
        self._responses = iter(responses)
        self.open_count = 0

    async def _open_event_stream(
        self,
    ) -> _FakeSseResponse | _BlockingSseResponse | _KeepaliveSseResponse:
        self.open_count += 1
        try:
            response = next(self._responses)
        except StopIteration as exc:
            raise AssertionError("Unexpected _open_event_stream call in test") from exc
        if isinstance(response, Exception):
            raise response
        return response


class _BlockingOpenStreamConnection(_LivenessProbeOpenCodeConnection):
    def __init__(self) -> None:
        super().__init__(responses=[])

    async def _open_event_stream(self) -> _FakeSseResponse | _BlockingSseResponse:
        self.open_count += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def _no_sleep(_delay: float) -> None:
    return None


async def _collect_opencode_events(connection: OpenCodeConnection) -> list[HarnessEvent]:
    return [event async for event in connection.events()]


def _build_connection_config(tmp_path: Path) -> ConnectionConfig:
    return ConnectionConfig(
        spawn_id=SpawnId("p-open-observer"),
        harness_id=HarnessId.OPENCODE,
        prompt="hello from test",
        control_root=tmp_path,
        env_overrides={"MERIDIAN_TEST_ENV": "1"},
        system="system from test",
    )


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

    monkeypatch.setattr(OpenCodeConnection, "_LIVENESS_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(OpenCodeConnection, "_EVENT_RETRY_DELAY_SECONDS", 0.02)

    events = [event async for event in connection.events()]

    assert events == []
    assert connection.open_count > 1
    assert connection.state == "failed"


@pytest.mark.asyncio
async def test_opencode_events_fail_after_liveness_timeout_opening_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _BlockingOpenStreamConnection()

    monkeypatch.setattr(OpenCodeConnection, "_LIVENESS_TIMEOUT_SECONDS", 0.01)

    events = [event async for event in connection.events()]

    assert events == []
    assert connection.open_count == 1
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
async def test_opencode_keepalive_chunks_do_not_refresh_liveness_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _LivenessProbeOpenCodeConnection(responses=[_KeepaliveSseResponse()])

    monkeypatch.setattr(OpenCodeConnection, "_LIVENESS_TIMEOUT_SECONDS", 0.03)

    events = await asyncio.wait_for(_collect_opencode_events(connection), timeout=1.0)

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
    clock = FakeClock(start=0.0)

    def advancing_monotonic() -> float:
        current = clock.monotonic()
        clock.advance(0.05)
        return current

    monkeypatch.setattr(OpenCodeConnection, "_LIVENESS_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(OpenCodeConnection, "_EVENT_RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(opencode_http.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(opencode_http.time, "monotonic", advancing_monotonic)

    events = [event async for event in connection.events()]

    assert [event.event_type for event in events] == ["session.idle"]
    assert connection.open_count >= 1
    assert connection.state == "failed"


def test_opencode_health_fails_when_event_liveness_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = OpenCodeConnection()
    connection._state = "connected"
    connection._process = _FakeProcess()
    connection._last_health_ok = True

    monkeypatch.setattr(OpenCodeConnection, "_LIVENESS_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(opencode_http.time, "monotonic", lambda: 10.0)
    connection._liveness.mark_activity()
    monkeypatch.setattr(opencode_http.time, "monotonic", lambda: 10.5)
    assert connection.health() is True

    monkeypatch.setattr(opencode_http.time, "monotonic", lambda: 11.1)
    assert connection.health() is False


@pytest.mark.asyncio
async def test_opencode_start_resets_expired_liveness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection = _StartProbeOpenCodeConnection()
    connection._state = "failed"

    monkeypatch.setattr(OpenCodeConnection, "_LIVENESS_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(opencode_http.time, "monotonic", lambda: 10.0)
    connection._liveness.mark_activity()
    monkeypatch.setattr(opencode_http.time, "monotonic", lambda: 12.0)
    assert connection.health() is False

    await connection.start(
        _build_connection_config(tmp_path),
        ResolvedLaunchSpec(
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    assert connection.health() is True


@pytest.mark.asyncio
async def test_opencode_session_creation_falls_back_when_projected_payload_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _PayloadTimeoutOpenCodeConnection()
    connection._process = _FakeProcess()
    monkeypatch.setattr(OpenCodeConnection, "_SESSION_CREATE_PAYLOAD_TIMEOUT_SECONDS", 0.01)

    session_id = await connection._create_session_with_retry(
        ResolvedLaunchSpec(
            model="gpt-5.5",
            agent_name="prober",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
        timeout_seconds=1.0,
    )

    assert session_id == "sess-empty-fallback"
    assert connection.payloads == [
        {"model": "gpt-5.5", "modelID": "gpt-5.5", "agent": "prober"},
        {},
    ]


@pytest.mark.asyncio
async def test_opencode_session_startup_timeout_wraps_hung_session_request() -> None:
    connection = _HangingSessionOpenCodeConnection()
    connection._process = _FakeProcess()

    with pytest.raises(TimeoutError, match=r"did not become ready within 0\.0s"):
        await connection._create_session_with_retry(
            ResolvedLaunchSpec(
                permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
            ),
            timeout_seconds=0.01,
        )


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
    assert connection.ready_calls == 1
    assert connection.create_session_calls == 1
    assert connection.initial_messages == expected_initial_messages

    await connection.stop()


@pytest.mark.asyncio
async def test_opencode_readiness_gate_succeeds_before_session_create(
    tmp_path: Path,
) -> None:
    connection = _StartProbeOpenCodeConnection()

    await connection.start(
        _build_connection_config(tmp_path),
        ResolvedLaunchSpec(
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    assert connection.state == "connected"

    await connection.stop()


@pytest.mark.asyncio
async def test_opencode_readiness_gate_retries_transient_timeout_before_session_create(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection = _ReadinessStartupProbeOpenCodeConnection(
        get_responses=[
            TimeoutError("backend still warming"),
            (200, {"status": "ok"}, "application/json"),
        ],
    )
    monkeypatch.setattr(opencode_http.asyncio, "sleep", _no_sleep)

    await connection.start(
        _build_connection_config(tmp_path),
        ResolvedLaunchSpec(
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    assert connection.state == "connected"
    assert connection.session_id == "sess-after-readiness-timeout"
    assert [path for path, _payload in connection.requests] == [
        "/global/health",
        "/global/health",
    ]

    await connection.stop()


@pytest.mark.asyncio
async def test_opencode_readiness_gate_timeout_is_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _TestableOpenCodeConnection(
        responses=[],
        get_responses=[
            (503, {"status": "warming"}, ""),
        ],
    )
    clock = FakeClock(start=0.0)

    def advancing_monotonic() -> float:
        current = clock.monotonic()
        clock.advance(0.08)
        return current

    monkeypatch.setattr(opencode_http.time, "monotonic", advancing_monotonic)
    monkeypatch.setattr(opencode_http.asyncio, "sleep", _no_sleep)

    with pytest.raises(TimeoutError, match=r"readiness endpoint did not become ready"):
        await connection._wait_for_ready(timeout_seconds=0.1)

    assert [path for path, _payload in connection.requests] == ["/global/health"]


@pytest.mark.asyncio
async def test_opencode_start_reports_session_id_when_connection_starts(tmp_path: Path) -> None:
    connection = _StartProbeOpenCodeConnection()
    observed: list[str] = []
    config = ConnectionConfig(
        spawn_id=SpawnId("p-open-observer"),
        harness_id=HarnessId.OPENCODE,
        prompt="hello from test",
        control_root=tmp_path,
        env_overrides={},
        session_id_observer=observed.append,
    )

    await connection.start(
        config,
        ResolvedLaunchSpec(
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    assert observed == ["sess-primary-observer"]
    assert connection.initial_messages == [("hello from test", None)]

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
async def test_opencode_launch_process_passes_env_overrides_to_managed_backend(
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

    async def _fake_launch_managed_backend(
        backend_config: object,
        *,
        stderr: object,
    ) -> object:
        captured["backend_config"] = backend_config
        captured["stderr"] = stderr
        snapshot = ProcessScopeSnapshot(
            scope_id="backend",
            owner_policy="spawn_owned",
            owner_id=str(config.spawn_id),
            role="harness_backend",
            containment="pid_tree_fallback",
            root_pid=fake_process.pid,
            root_created_at_epoch=1.0,
            pgid=None,
            job_name=None,
            degraded_reason=None,
            parent_death_linked=False,
        )
        return SimpleNamespace(
            process=fake_process,
            scope_handle=ScopedProcessHandle(process=fake_process, snapshot=snapshot),
            parent_death_link=ParentDeathLink(parent_death_linked=False),
        )

    monkeypatch.setattr(opencode_http, "_find_free_port", lambda _host="127.0.0.1": 17777)
    monkeypatch.setattr(opencode_http, "inherit_child_env", _fake_inherit_child_env)
    monkeypatch.setattr(opencode_http, "launch_managed_backend", _fake_launch_managed_backend)

    await connection._launch_process(config, spec)

    assert captured["overrides"] == config.env_overrides
    backend_config = captured["backend_config"]
    assert backend_config.cwd == config.control_root
    assert backend_config.control_root == config.control_root
    assert backend_config.command == (
        "opencode",
        "serve",
        "--hostname",
        "127.0.0.1",
        "--port",
        "17777",
    )
    assert backend_config.env["MERIDIAN_INHERIT_CALLED"] == "1"
    assert backend_config.env["MERIDIAN_TEST_ENV"] == "1"
    assert "OPENCODE_CONFIG_CONTENT" in backend_config.env

    oc_config = json.loads(backend_config.env["OPENCODE_CONFIG_CONTENT"])
    assert len(oc_config["instructions"]) == 1
    instruction_path = Path(oc_config["instructions"][0])
    assert instruction_path.parent == Path(tempfile.gettempdir())
    assert instruction_path.name.startswith("meridian-sysprompt-")
    assert instruction_path.suffix == ".md"
    assert connection.subprocess_pid == fake_process.pid

    await connection._cleanup_runtime()
