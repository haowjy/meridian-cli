# qa-validated: test-suite-redesign
"""Tests for OpenCodeConnection lifecycle, liveness, startup, and launch."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
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
from meridian.lib.state.paths import resolve_spawn_log_dir
from tests.support.async_determinism import AsyncDeterminism
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
        self.start_order: list[str] = []

    async def _launch_process(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = config, spec
        self.start_order.append("launch")
        self._process = _FakeProcess()

    async def _wait_for_ready(self, *, timeout_seconds: float) -> None:
        _ = timeout_seconds
        self.start_order.append("ready")

    async def _create_session_with_retry(
        self,
        spec: ResolvedLaunchSpec,
        *,
        timeout_seconds: float,
    ) -> str:
        _ = spec, timeout_seconds
        self.start_order.append("session")
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
        self.requests.append(("launch", {}))
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
        self.requests.append(("session", {}))
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


def _install_opencode_determinism(monkeypatch: pytest.MonkeyPatch) -> AsyncDeterminism:
    determinism = AsyncDeterminism(start=0.0)
    determinism.install(monkeypatch, monotonic_modules=(opencode_http,))
    return determinism


async def _collect_events_under_fake_clock(
    determinism: AsyncDeterminism,
    connection: OpenCodeConnection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    advance_budget: float = 1.0,
    step: float = 0.01,
) -> list[HarnessEvent]:
    determinism.install_on_running_loop(monkeypatch)
    task = asyncio.create_task(_collect_opencode_events(connection))
    advanced = 0.0
    while not task.done() and advanced < advance_budget:
        await determinism.sleep(step)
        advanced += step
    assert task.done(), (
        f"event collection did not finish within fake-clock budget {advance_budget}"
    )
    return await task


def _build_connection_config(tmp_path: Path) -> ConnectionConfig:
    return ConnectionConfig(
        spawn_id=SpawnId("p-open-observer"),
        harness_id=HarnessId.OPENCODE,
        prompt="hello from test",
        control_root=tmp_path,
        env_overrides={"MERIDIAN_TEST_ENV": "1"},
        system="system from test",
    )


@pytest.mark.asyncio
async def test_opencode_events_fail_after_liveness_timeout_without_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    determinism = _install_opencode_determinism(monkeypatch)
    connection = _LivenessProbeOpenCodeConnection(
        responses=[_FakeSseResponse([]) for _ in range(100)]
    )

    monkeypatch.setattr(OpenCodeConnection, "_LIVENESS_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(OpenCodeConnection, "_EVENT_RETRY_DELAY_SECONDS", 0.02)

    events = await _collect_events_under_fake_clock(determinism, connection, monkeypatch)

    assert events == []
    assert connection.open_count > 1
    assert connection.state == "failed"


@pytest.mark.asyncio
async def test_opencode_events_fail_after_liveness_timeout_opening_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    determinism = _install_opencode_determinism(monkeypatch)
    connection = _BlockingOpenStreamConnection()

    monkeypatch.setattr(OpenCodeConnection, "_LIVENESS_TIMEOUT_SECONDS", 0.01)

    events = await _collect_events_under_fake_clock(determinism, connection, monkeypatch)

    assert events == []
    assert connection.open_count == 1
    assert connection.state == "failed"


@pytest.mark.asyncio
async def test_opencode_events_fail_when_open_stream_stays_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    determinism = _install_opencode_determinism(monkeypatch)
    connection = _LivenessProbeOpenCodeConnection(responses=[_BlockingSseResponse()])

    monkeypatch.setattr(OpenCodeConnection, "_LIVENESS_TIMEOUT_SECONDS", 0.01)

    events = await _collect_events_under_fake_clock(determinism, connection, monkeypatch)

    assert events == []
    assert connection.open_count == 1
    assert connection.state == "failed"


@pytest.mark.asyncio
async def test_opencode_keepalive_chunks_do_not_refresh_liveness_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    determinism = _install_opencode_determinism(monkeypatch)
    connection = _LivenessProbeOpenCodeConnection(responses=[_KeepaliveSseResponse()])

    monkeypatch.setattr(OpenCodeConnection, "_LIVENESS_TIMEOUT_SECONDS", 0.03)

    events = await _collect_events_under_fake_clock(determinism, connection, monkeypatch)

    assert events == []
    assert connection.open_count == 1
    assert connection.state == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type",
    (OPENCODE_ACTIVITY_IDLE_EVENT, OPENCODE_ACTIVITY_ERROR_EVENT),
)
async def test_opencode_events_surface_activity_transition_events(
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
) -> None:
    connection = _LivenessProbeOpenCodeConnection(
        responses=[
            _FakeSseResponse(
                [f'{{"type":"{event_type}","sessionID":"sess-liveness"}}\n'.encode()]
            ),
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

    assert [event.event_type for event in events] == [event_type]
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
    determinism = _install_opencode_determinism(monkeypatch)
    determinism.install_on_running_loop(monkeypatch)
    connection = _PayloadTimeoutOpenCodeConnection()
    connection._process = _FakeProcess()
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
    session_id = await create_task

    assert session_id == "sess-empty-fallback"
    assert connection.payloads == [
        {"model": "gpt-5.5", "modelID": "gpt-5.5", "agent": "prober"},
        {},
    ]


@pytest.mark.asyncio
async def test_opencode_session_startup_timeout_wraps_hung_session_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    determinism = _install_opencode_determinism(monkeypatch)
    determinism.install_on_running_loop(monkeypatch)
    connection = _HangingSessionOpenCodeConnection()
    connection._process = _FakeProcess()

    create_task = asyncio.create_task(
        connection._create_session_with_retry(
            ResolvedLaunchSpec(
                permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
            ),
            timeout_seconds=0.01,
        )
    )
    while not create_task.done():
        await determinism.sleep(0.01)

    with pytest.raises(TimeoutError, match=r"did not become ready within 0\.0s"):
        await create_task


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
    assert connection.start_order == ["launch", "ready", "session"]

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
    paths = [path for path, _payload in connection.requests]
    session_index = paths.index("session")
    assert paths[0] == "launch"
    assert "/global/health" in paths[:session_index]
    assert paths[-1] == "session"

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

    def _fake_build_connection_child_env(**kwargs: object) -> dict[str, str]:
        overrides = kwargs.get("env_overrides")
        assert isinstance(overrides, dict)
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
    monkeypatch.setattr(
        opencode_http,
        "build_connection_child_env",
        _fake_build_connection_child_env,
    )
    monkeypatch.setattr(opencode_http, "launch_managed_backend", _fake_launch_managed_backend)

    await connection._launch_process(config, spec)

    backend_config = captured["backend_config"]
    assert captured["overrides"] == config.env_overrides
    assert backend_config.env["MERIDIAN_INHERIT_CALLED"] == "1"
    assert backend_config.env["MERIDIAN_TEST_ENV"] == "1"
    assert connection.subprocess_pid == fake_process.pid

    await connection._cleanup_runtime()


class _EarlyExitStartupOpenCodeConnection(OpenCodeConnection):
    def __init__(self, *, stderr_text: str) -> None:
        super().__init__()
        self._startup_stderr_text = stderr_text

    async def _launch_process(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = spec
        spawn_dir = resolve_spawn_log_dir(config.control_root, config.spawn_id)
        spawn_dir.mkdir(parents=True, exist_ok=True)
        self._stderr_log_path = spawn_dir / "stderr.log"
        self._stderr_log_path.write_text(self._startup_stderr_text, encoding="utf-8")
        self._stderr_read_offset = 0
        self._process = _FakeProcess()
        self._process.returncode = 1


def _write_startup_stderr(connection: OpenCodeConnection, tmp_path: Path, text: str) -> None:
    spawn_dir = tmp_path / "spawns" / "p-startup-fail"
    spawn_dir.mkdir(parents=True, exist_ok=True)
    connection._stderr_log_path = spawn_dir / "stderr.log"
    connection._stderr_log_path.write_text(text, encoding="utf-8")
    connection._stderr_read_offset = 0
    connection._process = _FakeProcess()
    connection._process.returncode = 1
    connection._config = _build_connection_config(tmp_path)


@pytest.mark.asyncio
async def test_opencode_startup_exit_surfaces_stderr_and_xdg_hint(tmp_path: Path) -> None:
    connection = _EarlyExitStartupOpenCodeConnection(
        stderr_text=(
            "EACCES: permission denied, mkdir '/root/meridian-probe-opencode-no-access/opencode'\n"
        ),
    )
    config = _build_connection_config(tmp_path)
    config = replace(
        config,
        env_overrides={"XDG_DATA_HOME": "/root/meridian-probe-opencode-no-access"},
    )

    with pytest.raises(RuntimeError) as exc_info:
        await connection.start(
            config,
            ResolvedLaunchSpec(
                permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
            ),
        )

    message = str(exc_info.value)
    assert "OpenCode backend failed to start (exit=1)" in message
    assert "EACCES: permission denied" in message
    assert "Hint:" in message
    assert "XDG_DATA_HOME (/root/meridian-probe-opencode-no-access)" in message
    assert connection.state == "failed"


def test_opencode_startup_exit_message_without_stderr(tmp_path: Path) -> None:
    connection = OpenCodeConnection()
    _write_startup_stderr(connection, tmp_path, "")

    message = str(connection._startup_exit_exception())

    assert message == "OpenCode backend failed to start (exit=1)."


def test_spawn_action_output_omits_transcript_when_session_log_unavailable() -> None:
    from meridian.lib.ops.spawn.models import SpawnActionOutput

    output = SpawnActionOutput(
        command="spawn.create",
        status="failed",
        spawn_id="p4735",
        session_log_available=False,
        duration_secs=0.5,
    )

    text = output.format_text()
    assert "Transcript:" not in text
    assert output.to_wire().get("transcript_command") is None
