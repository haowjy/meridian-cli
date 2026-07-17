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
from meridian.lib.harness.semantics import terminal_outcome
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.platform.detached_process import ParentDeathLink
from meridian.lib.platform.process_scope import ProcessScopeSnapshot, ScopedProcessHandle
from meridian.lib.safety.permissions import (
    UnsafeNoOpPermissionResolver,
)
from meridian.lib.state.paths import resolve_spawn_log_dir
from tests.support.async_determinism import AsyncDeterminism
from tests.support.fakes import FakeClock
from tests.support.opencode import FakeOpenCodeProcess

OPENCODE_ACTIVITY_IDLE_EVENT = "session.idle"
OPENCODE_ACTIVITY_ERROR_EVENT = "session.error"


class _StartProbeOpenCodeConnection(OpenCodeConnection):
    def __init__(
        self,
        get_responses: list[tuple[int, object | None, str] | Exception] | None = None,
    ) -> None:
        super().__init__()
        self.initial_messages: list[tuple[str, str | None]] = []
        self.startup_events: list[str] = []
        self._get_responses = iter(get_responses) if get_responses is not None else None

    async def _get_json(
        self,
        path: str,
        *,
        timeout: float | None = None,
    ) -> tuple[int, object | None, str]:
        _ = timeout
        self.startup_events.append(f"health:{path}")
        if self._get_responses is None:
            raise AssertionError("Unexpected readiness probe")
        try:
            response = next(self._get_responses)
        except StopIteration as exc:
            raise AssertionError("Unexpected readiness probe") from exc
        if isinstance(response, Exception):
            raise response
        return response

    async def _launch_process(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = config, spec
        self.startup_events.append("launch")
        self._process = FakeOpenCodeProcess()

    async def _wait_for_ready(self, *, timeout_seconds: float) -> None:
        if self._get_responses is not None:
            await super()._wait_for_ready(timeout_seconds=timeout_seconds)
        self.startup_events.append("ready")

    async def _create_session_with_retry(
        self,
        spec: ResolvedLaunchSpec,
        *,
        timeout_seconds: float,
    ) -> str:
        _ = spec, timeout_seconds
        self.startup_events.append("session")
        return "sess-primary-observer"

    async def _post_session_message(self, text: str, *, system: str | None = None) -> None:
        self.initial_messages.append((text, system))


class _SseContent:
    def __init__(
        self,
        chunks: list[bytes] | None = None,
        *,
        block: bool = False,
        keepalive: bool = False,
    ) -> None:
        self._chunks = list(chunks or ())
        self._block = block
        self._keepalive = keepalive

    async def read(self, _size: int) -> bytes:
        if self._block:
            await asyncio.Event().wait()
        if self._keepalive:
            await asyncio.sleep(0.001)
            return b": keepalive\n\n"
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    async def iter_chunked(self, _size: int):  # type: ignore[no-untyped-def]
        for chunk in self._chunks:
            yield chunk


class _SseResponse:
    def __init__(
        self,
        chunks: list[bytes] | None = None,
        *,
        block: bool = False,
        keepalive: bool = False,
    ) -> None:
        self.content = _SseContent(chunks, block=block, keepalive=keepalive)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _ProcessDeathSseContent:
    def __init__(self, process: FakeOpenCodeProcess, return_code: int) -> None:
        self._process = process
        self._return_code = return_code
        self._sent_event = False

    async def read(self, _size: int) -> bytes:
        if not self._sent_event:
            self._sent_event = True
            return b'{"type":"session.updated","sessionID":"sess-liveness"}\n'
        self._process.exit(self._return_code)
        return b""


class _ProcessDeathSseResponse:
    def __init__(self, process: FakeOpenCodeProcess, return_code: int) -> None:
        self.content = _ProcessDeathSseContent(process, return_code)

    def close(self) -> None:
        return None


class _LivenessProbeOpenCodeConnection(OpenCodeConnection):
    def __init__(
        self,
        responses: list[_SseResponse | _ProcessDeathSseResponse | Exception],
        *,
        block_open: bool = False,
    ) -> None:
        super().__init__()
        self._state = "connected"
        self._session_id = "sess-liveness"
        self._process = FakeOpenCodeProcess()
        self._responses = iter(responses)
        self._block_open = block_open
        self.open_count = 0

    async def _open_event_stream(self) -> _SseResponse:
        self.open_count += 1
        if self._block_open:
            await asyncio.Event().wait()
        try:
            response = next(self._responses)
        except StopIteration as exc:
            raise AssertionError("Unexpected _open_event_stream call in test") from exc
        if isinstance(response, Exception):
            raise response
        return response


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
        responses=[_SseResponse() for _ in range(100)]
    )

    monkeypatch.setattr(OpenCodeConnection, "_LIVENESS_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(OpenCodeConnection, "_EVENT_RETRY_DELAY_SECONDS", 0.02)

    events = await _collect_events_under_fake_clock(determinism, connection, monkeypatch)

    assert events == []
    assert connection.open_count > 1
    assert connection.state == "failed"


@pytest.mark.asyncio
async def test_opencode_process_death_emits_terminal_outcome_with_exit_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = FakeOpenCodeProcess()
    response = _ProcessDeathSseResponse(process, return_code=17)
    connection = _LivenessProbeOpenCodeConnection(responses=[response])
    connection._process = process
    connection._stderr_log_path = tmp_path / "stderr.log"
    connection._stderr_log_path.write_text("fatal backend detail\n", encoding="utf-8")
    connection._stderr_read_offset = 0

    monkeypatch.setattr(OpenCodeConnection, "_EVENT_RETRY_DELAY_SECONDS", 0.0)

    events = await _collect_opencode_events(connection)

    assert [event.event_type for event in events] == [
        "session.updated",
        "error/connectionClosed",
    ]
    outcome = terminal_outcome(
        events[-1],
        primary_event_scope=connection.primary_event_scope,
    )
    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.error == (
        "OpenCode subprocess exited with code 17.\n\n"
        "OpenCode subprocess stderr:\n"
        "fatal backend detail"
    )


@pytest.mark.asyncio
async def test_opencode_events_fail_after_liveness_timeout_opening_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    determinism = _install_opencode_determinism(monkeypatch)
    connection = _LivenessProbeOpenCodeConnection(responses=[], block_open=True)

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
    connection = _LivenessProbeOpenCodeConnection(responses=[_SseResponse(block=True)])

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
    connection = _LivenessProbeOpenCodeConnection(responses=[_SseResponse(keepalive=True)])

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
            _SseResponse(
                [f'{{"type":"{event_type}","sessionID":"sess-liveness"}}\n'.encode()]
            ),
            _SseResponse(),
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
    connection._process = FakeOpenCodeProcess()
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
    assert connection.startup_events == ["launch", "ready", "session"]

    await connection.stop()


@pytest.mark.asyncio
async def test_opencode_readiness_gate_retries_transient_timeout_before_session_create(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection = _StartProbeOpenCodeConnection(
        [
            TimeoutError("backend still warming"),
            (200, {"status": "ok"}, "application/json"),
        ]
    )
    monkeypatch.setattr(opencode_http.asyncio, "sleep", _no_sleep)

    await connection.start(
        _build_connection_config(tmp_path),
        ResolvedLaunchSpec(
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    assert connection.state == "connected"
    assert connection.startup_events == [
        "launch",
        "health:/global/health",
        "health:/global/health",
        "ready",
        "session",
    ]

    await connection.stop()


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
    fake_process = FakeOpenCodeProcess()
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

    backend_config = captured["backend_config"]
    assert captured["overrides"] == config.env_overrides
    assert backend_config.env["MERIDIAN_INHERIT_CALLED"] == "1"
    assert backend_config.env["MERIDIAN_TEST_ENV"] == "1"
    assert connection.subprocess_pid == fake_process.pid

    await connection._cleanup_runtime()


def _set_startup_failure(
    connection: OpenCodeConnection,
    config: ConnectionConfig,
    text: str,
) -> None:
    spawn_dir = resolve_spawn_log_dir(config.control_root, config.spawn_id)
    spawn_dir.mkdir(parents=True, exist_ok=True)
    connection._stderr_log_path = spawn_dir / "stderr.log"
    connection._stderr_log_path.write_text(text, encoding="utf-8")
    connection._stderr_read_offset = 0
    connection._process = FakeOpenCodeProcess()
    connection._process.returncode = 1
    connection._config = config


@pytest.mark.asyncio
async def test_opencode_startup_exit_surfaces_stderr_and_xdg_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = OpenCodeConnection()
    config = replace(
        _build_connection_config(tmp_path),
        env_overrides={"XDG_DATA_HOME": "/root/meridian-probe-opencode-no-access"},
    )

    async def launch_failure(
        launch_config: ConnectionConfig,
        _spec: ResolvedLaunchSpec,
    ) -> None:
        _set_startup_failure(
            connection,
            launch_config,
            "EACCES: permission denied, mkdir "
            "'/root/meridian-probe-opencode-no-access/opencode'\n",
        )

    monkeypatch.setattr(connection, "_launch_process", launch_failure)

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
    _set_startup_failure(connection, _build_connection_config(tmp_path), "")

    message = str(connection._startup_exit_exception())

    assert message == "OpenCode backend failed to start (exit=1)."
