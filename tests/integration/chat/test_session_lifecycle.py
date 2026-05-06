from __future__ import annotations

import pytest

from meridian.lib.chat.session_service import ChatSessionService
from meridian.lib.core.types import SpawnId


class Handle:
    def __init__(self, spawn_id: str, *, healthy: bool = True) -> None:
        self.spawn_id = SpawnId(spawn_id)
        self._healthy = healthy
        self.messages: list[str] = []
        self.cancel_calls = 0
        self.stop_calls = 0

    def health(self) -> bool:
        return self._healthy

    async def send_message(self, text: str) -> None:
        self.messages.append(text)

    async def send_cancel(self) -> None:
        self.cancel_calls += 1

    async def stop(self) -> None:
        self.stop_calls += 1


class Acquisition:
    def __init__(self, handles: list[Handle]) -> None:
        self._handles = iter(handles)
        self.calls: list[tuple[str, str, int]] = []

    async def acquire(
        self,
        chat_id,
        initial_prompt,
        *,
        execution_generation=0,
        chat_state="active",
    ):
        _ = chat_state
        self.calls.append((chat_id, initial_prompt, execution_generation))
        return next(self._handles)


@pytest.mark.asyncio
async def test_idle_active_draining_idle_closed_lifecycle() -> None:
    session = ChatSessionService("c1", Acquisition([Handle("s1")]))
    assert session.state == "idle"
    await session.prompt("hi")
    assert session.state == "active"
    await session.cancel()
    assert session.state == "draining"
    session.on_turn_completed()
    assert session.state == "idle"
    await session.close()
    assert session.state == "closed"


@pytest.mark.asyncio
async def test_dead_execution_is_reacquired_on_next_prompt() -> None:
    first = Handle("s1", healthy=False)
    second = Handle("s2")
    acquisition = Acquisition([first, second])
    session = ChatSessionService("c1", acquisition)

    await session.prompt("first")
    session.on_turn_completed()
    assert session.current_execution is first
    assert session.execution_generation == 1

    await session.prompt("second")

    assert acquisition.calls == [("c1", "first", 1), ("c1", "second", 2)]
    assert session.current_execution is second
    assert session.execution_generation == 2
    assert session.state == "active"


@pytest.mark.asyncio
async def test_cancel_is_idempotent_outside_active_state() -> None:
    handle = Handle("s1")
    session = ChatSessionService("c1", Acquisition([handle]))

    await session.cancel()
    assert handle.cancel_calls == 0
    assert session.state == "idle"

    await session.prompt("hi")
    await session.cancel()
    assert handle.cancel_calls == 1
    assert session.state == "draining"

    await session.cancel()
    assert handle.cancel_calls == 1
    assert session.state == "draining"

    session.on_turn_completed()
    await session.cancel()
    assert handle.cancel_calls == 1
    assert session.state == "idle"
