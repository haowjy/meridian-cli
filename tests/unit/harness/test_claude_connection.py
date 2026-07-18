"""Claude streaming connection event-shape tests."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from meridian.lib.harness.connections.claude_ws import ClaudeConnection
from meridian.lib.harness.semantics import normalize_event


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

    assert event.event_type == "meridian/error/connectionClosed"
    assert event.payload == {
        "type": "meridian/error/connectionClosed",
        "message": (
            "Claude subprocess exited with code 17.\n\n"
            "Claude subprocess stderr:\n"
            "fatal claude backend detail"
        ),
    }
    outcome = normalize_event(event).semantics.terminal
    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.error == event.payload["message"]
    with pytest.raises(StopAsyncIteration):
        await anext(events)
