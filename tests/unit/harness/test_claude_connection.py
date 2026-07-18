"""Claude streaming connection event-shape tests."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from meridian.lib.harness.connections.claude_ws import ClaudeConnection
from meridian.lib.harness.semantics import terminal_outcome


class _VersionProcess:
    async def communicate(self) -> tuple[bytes, bytes]:
        return b"2.1.0\n", b""


class _FakeStdout:
    async def readline(self) -> bytes:
        return b""


class _ExitedProcess:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.stdout = _FakeStdout()

    async def wait(self) -> int:
        return self.returncode


@pytest.mark.asyncio
async def test_claude_version_probe_uses_bound_child_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_env: dict[str, str] = {}

    async def fake_create_subprocess_exec(
        *_command: str,
        env: dict[str, str],
        **_kwargs: object,
    ) -> _VersionProcess:
        captured_env.update(env)
        return _VersionProcess()

    monkeypatch.setattr(
        "meridian.lib.harness.connections.claude_ws.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    child_env = {"PATH": "/bound/bin", "HOME": "/bound/home"}

    await ClaudeConnection()._check_claude_version(child_env)  # pyright: ignore[reportPrivateUsage]

    assert captured_env == child_env
    assert captured_env is not child_env


@pytest.mark.asyncio
async def test_claude_process_death_emits_diagnostic_event_before_eof(
    tmp_path: Path,
) -> None:
    stderr_path = tmp_path / "stderr.log"
    stderr_path.write_text("fatal claude backend detail\n", encoding="utf-8")
    connection = ClaudeConnection()
    connection._state = "connected"  # pyright: ignore[reportPrivateUsage]
    connection._process = cast("object", _ExitedProcess(17))  # type: ignore[assignment]
    connection._stderr_log_path = stderr_path  # pyright: ignore[reportPrivateUsage]
    events = connection.events()

    event = await anext(events)

    assert event.event_type == "error/connectionClosed"
    assert event.payload == {
        "type": "error/connectionClosed",
        "message": (
            "Claude subprocess exited with code 17.\n\n"
            "Claude subprocess stderr:\n"
            "fatal claude backend detail"
        ),
    }
    outcome = terminal_outcome(event)
    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.error == event.payload["message"]
    with pytest.raises(StopAsyncIteration):
        await anext(events)
