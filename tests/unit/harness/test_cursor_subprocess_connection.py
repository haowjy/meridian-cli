"""Cursor subprocess connection parsing and shutdown tests."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections import cursor_subprocess as cursor_subprocess_module
from meridian.lib.harness.connections.base import ConnectionConfig, HarnessEvent
from meridian.lib.harness.connections.cursor_subprocess import CursorSubprocessConnection
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state.paths import resolve_spawn_log_dir


class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""


class _FakeProcess:
    def __init__(
        self,
        *,
        lines: list[bytes],
        returncode: int | None,
        wait_returncode: int = 0,
        kill_error: BaseException | None = None,
    ) -> None:
        self.stdout = _FakeStdout(lines)
        self.returncode = returncode
        self.wait_returncode = wait_returncode
        self.kill_error = kill_error
        self.terminate_called = False
        self.kill_called = False
        self.wait_calls = 0
        self.pid = 42

    async def wait(self) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            self.returncode = self.wait_returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminate_called = True

    def kill(self) -> None:
        self.kill_called = True
        if self.kill_error is not None:
            raise self.kill_error


async def _collect_events(connection: CursorSubprocessConnection) -> list[HarnessEvent]:
    return [event async for event in connection.events()]


def _passthrough_child_env(
    _base: object,
    overrides: dict[str, str],
    blocked: object,
) -> dict[str, str]:
    _ = blocked
    return dict(overrides)


@pytest.mark.asyncio
async def test_cursor_events_skip_invalid_lines_after_protocol_validation() -> None:
    connection = CursorSubprocessConnection()
    connection._state = "connected"  # pyright: ignore[reportPrivateUsage]
    connection._process = _FakeProcess(  # pyright: ignore[reportPrivateUsage]
        lines=[
            b'{"type":"system","sessionId":"ses-1"}\n',
            b'{"oops":\n',
            b'{"type":"result","subtype":"success"}\n',
        ],
        returncode=0,
    )

    events = await _collect_events(connection)

    assert [event.event_type for event in events] == ["system", "result"]
    assert connection.state == "connected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_line",
    [
        b'{"oops":\n',
        b'["not-object"]\n',
        b'{"message":"missing type"}\n',
    ],
)
async def test_cursor_events_emit_protocol_mismatch_when_first_payload_invalid(
    bad_line: bytes,
) -> None:
    connection = CursorSubprocessConnection()
    connection._state = "connected"  # pyright: ignore[reportPrivateUsage]
    connection._process = _FakeProcess(  # pyright: ignore[reportPrivateUsage]
        lines=[bad_line],
        returncode=0,
    )

    events = await _collect_events(connection)

    assert len(events) == 1
    assert events[0].event_type == "error/connectionClosed"
    assert "protocol mismatch" in str(events[0].payload.get("message", "")).lower()
    assert connection.state == "failed"


@pytest.mark.asyncio
async def test_cursor_events_emit_error_on_nonzero_exit() -> None:
    connection = CursorSubprocessConnection()
    connection._state = "connected"  # pyright: ignore[reportPrivateUsage]
    connection._process = _FakeProcess(  # pyright: ignore[reportPrivateUsage]
        lines=[],
        returncode=7,
    )

    events = await _collect_events(connection)

    assert len(events) == 1
    assert events[0].event_type == "error/connectionClosed"
    assert events[0].payload["message"] == "Cursor subprocess exited with code 7."
    assert connection.state == "failed"


@pytest.mark.asyncio
async def test_cursor_send_cancel_handles_process_lookup_kill_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _timeout_wait_for(
        awaitable: object,
        timeout: float,
    ) -> int:
        _ = timeout
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(
        "meridian.lib.harness.connections.cursor_subprocess.asyncio.wait_for",
        _timeout_wait_for,
    )

    process = _FakeProcess(
        lines=[],
        returncode=None,
        kill_error=ProcessLookupError(),
    )
    connection = CursorSubprocessConnection()
    connection._state = "connected"  # pyright: ignore[reportPrivateUsage]
    connection._process = process  # pyright: ignore[reportPrivateUsage]

    await connection.send_cancel()

    assert process.terminate_called is True
    assert process.kill_called is True
    assert connection.subprocess_pid is None


class _MintThenAgentProcess:
    def __init__(self, *, mint_stdout: bytes, returncode: int = 0) -> None:
        self.mint_stdout = mint_stdout
        self.returncode = returncode
        self.pid = 99
        self.stdout = _FakeStdout([])
        self.terminate_called = False
        self.kill_called = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.mint_stdout, b""

    def terminate(self) -> None:
        self.terminate_called = True

    def kill(self) -> None:
        self.kill_called = True

    async def wait(self) -> int:
        return self.returncode


class _AgentOnlyProcess:
    def __init__(self) -> None:
        self.pid = 100
        self.stdout = _FakeStdout([])
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _BlockingMintProcess:
    def __init__(self) -> None:
        self.pid = 99
        self.returncode: int | None = None
        self.kill_called = False

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(3600)
        return b"", b""

    def kill(self) -> None:
        self.kill_called = True
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = -9
        return self.returncode


async def _start_fresh_cursor_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    mint_process: _MintThenAgentProcess | _BlockingMintProcess,
    spawn_id: str = "p-cursor-mint-fail",
) -> tuple[CursorSubprocessConnection, list[str], list[tuple[str, ...]]]:
    observed: list[str] = []
    captured_commands: list[tuple[str, ...]] = []

    async def _fake_create_subprocess_exec(
        *command: str,
        **_kwargs: object,
    ) -> _MintThenAgentProcess | _BlockingMintProcess | _AgentOnlyProcess:
        captured_commands.append(tuple(command))
        if command[-1] == "create-chat":
            return mint_process
        return _AgentOnlyProcess()

    monkeypatch.setattr(
        cursor_subprocess_module,
        "inherit_child_env",
        _passthrough_child_env,
    )
    monkeypatch.setattr(
        cursor_subprocess_module.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    connection = CursorSubprocessConnection()
    config = ConnectionConfig(
        spawn_id=SpawnId(spawn_id),
        harness_id=HarnessId.CURSOR,
        prompt="hello",
        control_root=tmp_path,
        env_overrides={},
        session_id_observer=observed.append,
    )
    resolve_spawn_log_dir(tmp_path, config.spawn_id).mkdir(parents=True)
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CURSOR,
        prompt="hello",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    await connection.start(config, spec)
    return connection, observed, captured_commands


def _assert_create_chat_degraded_to_fresh_agent(
    connection: CursorSubprocessConnection,
    observed: list[str],
    captured_commands: list[tuple[str, ...]],
) -> None:
    assert connection.session_id is None
    assert observed == []
    assert len(captured_commands) == 2
    assert captured_commands[0] == ("cursor", "agent", "create-chat")
    agent_command = captured_commands[1]
    assert "--resume" not in agent_command
    assert agent_command[-1] == "hello"
    assert connection.state == "connected"


@pytest.mark.asyncio
async def test_cursor_start_mints_chat_id_and_records_observer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    minted_id = "550e8400-e29b-41d4-a716-446655440000"
    observed: list[str] = []
    captured_commands: list[tuple[str, ...]] = []

    async def _fake_create_subprocess_exec(
        *command: str,
        **_kwargs: object,
    ) -> _MintThenAgentProcess | _AgentOnlyProcess:
        captured_commands.append(tuple(command))
        if command[-1] == "create-chat":
            return _MintThenAgentProcess(mint_stdout=f"{minted_id}\n".encode())
        return _AgentOnlyProcess()

    monkeypatch.setattr(
        cursor_subprocess_module,
        "inherit_child_env",
        _passthrough_child_env,
    )
    monkeypatch.setattr(
        cursor_subprocess_module.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    connection = CursorSubprocessConnection()
    config = ConnectionConfig(
        spawn_id=SpawnId("p-cursor-mint"),
        harness_id=HarnessId.CURSOR,
        prompt="hello",
        control_root=tmp_path,
        env_overrides={},
        session_id_observer=observed.append,
    )
    resolve_spawn_log_dir(tmp_path, config.spawn_id).mkdir(parents=True)
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CURSOR,
        prompt="hello",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    await connection.start(config, spec)

    assert connection.session_id == minted_id
    assert observed == [minted_id]
    assert captured_commands[0] == ("cursor", "agent", "create-chat")
    agent_command = captured_commands[1]
    resume_idx = agent_command.index("--resume")
    assert agent_command[resume_idx + 1] == minted_id
    assert agent_command[-1] == "hello"
    assert connection.state == "connected"

    await connection.stop()


@pytest.mark.asyncio
async def test_cursor_start_reuses_continue_session_id_without_minting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    existing_id = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
    observed: list[str] = []
    captured_commands: list[tuple[str, ...]] = []

    async def _fake_create_subprocess_exec(
        *command: str,
        **_kwargs: object,
    ) -> _AgentOnlyProcess:
        captured_commands.append(tuple(command))
        return _AgentOnlyProcess()

    monkeypatch.setattr(
        cursor_subprocess_module,
        "inherit_child_env",
        _passthrough_child_env,
    )
    monkeypatch.setattr(
        cursor_subprocess_module.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    connection = CursorSubprocessConnection()
    config = ConnectionConfig(
        spawn_id=SpawnId("p-cursor-resume"),
        harness_id=HarnessId.CURSOR,
        prompt="hello",
        control_root=tmp_path,
        env_overrides={},
        session_id_observer=observed.append,
    )
    resolve_spawn_log_dir(tmp_path, config.spawn_id).mkdir(parents=True)
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CURSOR,
        prompt="hello",
        continue_session_id=existing_id,
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    await connection.start(config, spec)

    assert connection.session_id == existing_id
    assert observed == [existing_id]
    assert len(captured_commands) == 1
    agent_command = captured_commands[0]
    resume_idx = agent_command.index("--resume")
    assert agent_command[resume_idx + 1] == existing_id

    await connection.stop()


@pytest.mark.asyncio
async def test_cursor_start_degrades_when_create_chat_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection, observed, captured_commands = await _start_fresh_cursor_spawn(
        monkeypatch,
        tmp_path,
        mint_process=_MintThenAgentProcess(
            mint_stdout=b"ignored\n",
            returncode=1,
        ),
        spawn_id="p-cursor-mint-nonzero",
    )

    _assert_create_chat_degraded_to_fresh_agent(connection, observed, captured_commands)

    await connection.stop()


@pytest.mark.asyncio
async def test_cursor_start_degrades_when_create_chat_returns_non_uuid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection, observed, captured_commands = await _start_fresh_cursor_spawn(
        monkeypatch,
        tmp_path,
        mint_process=_MintThenAgentProcess(mint_stdout=b"not-a-uuid\n", returncode=0),
        spawn_id="p-cursor-mint-bad-uuid",
    )

    _assert_create_chat_degraded_to_fresh_agent(connection, observed, captured_commands)

    await connection.stop()


@pytest.mark.asyncio
async def test_cursor_start_degrades_when_create_chat_times_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    blocking_mint = _BlockingMintProcess()
    monkeypatch.setattr(
        cursor_subprocess_module,
        "_CREATE_CHAT_TIMEOUT_SECONDS",
        0.01,
    )

    connection, observed, captured_commands = await _start_fresh_cursor_spawn(
        monkeypatch,
        tmp_path,
        mint_process=blocking_mint,
        spawn_id="p-cursor-mint-timeout",
    )

    assert blocking_mint.kill_called is True
    _assert_create_chat_degraded_to_fresh_agent(connection, observed, captured_commands)

    await connection.stop()
