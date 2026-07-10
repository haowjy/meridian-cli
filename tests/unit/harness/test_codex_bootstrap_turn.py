from __future__ import annotations

import asyncio

import pytest

from meridian.lib.harness.connections.codex_ws import CodexConnection


@pytest.mark.asyncio
async def test_bootstrap_wait_finishes_only_after_matching_turn_completes() -> None:
    connection = CodexConnection()
    wait_task = asyncio.create_task(
        connection._wait_for_turn_completion("turn-1", timeout_seconds=1.0)
    )

    connection._update_turn_state(
        method="turn/started",
        payload={"threadId": "thread-1", "turn": {"id": "turn-1"}},
    )
    await asyncio.sleep(0)
    assert wait_task.done() is False

    connection._update_turn_state(
        method="turn/completed",
        payload={"threadId": "thread-1", "turn": {"id": "turn-1"}},
    )

    await asyncio.wait_for(wait_task, timeout=1.0)
    assert connection.current_turn_id is None
