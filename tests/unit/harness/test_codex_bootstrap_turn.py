from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from meridian.lib.harness.connections.codex_ws import CodexConnection


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_name", "expected_prompt"),
    [
        ("reviewer", "Meridian started (agent: reviewer)"),
        (None, "Meridian started"),
        ("  ", "Meridian started"),
    ],
)
async def test_bootstrap_turn_identifies_agent_when_available(
    agent_name: str | None,
    expected_prompt: str,
) -> None:
    connection = CodexConnection()
    connection._thread_id = "thread-1"
    connection._request = AsyncMock(return_value={"turn": {"id": "turn-1"}})  # type: ignore[method-assign]
    connection._wait_for_rollout_materialization = AsyncMock()  # type: ignore[method-assign]
    connection._wait_for_turn_completion = AsyncMock()  # type: ignore[method-assign]

    await connection._send_bootstrap_turn_and_wait(agent_name=agent_name)

    connection._request.assert_awaited_once_with(
        "turn/start",
        {
            "threadId": "thread-1",
            "input": [{"type": "text", "text": expected_prompt}],
        },
    )


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
