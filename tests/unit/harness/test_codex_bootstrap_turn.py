from __future__ import annotations

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

    await connection._send_bootstrap_turn_and_wait(agent_name=agent_name)

    connection._request.assert_awaited_once_with(
        "turn/start",
        {
            "threadId": "thread-1",
            "input": [{"type": "text", "text": expected_prompt}],
        },
    )


@pytest.mark.asyncio
async def test_bootstrap_does_not_wait_for_turn_completion() -> None:
    """Rollout materialization is the readiness boundary, not turn completion.

    The bootstrap turn may trigger real model work (tool calls, reasoning)
    that takes arbitrarily long. Waiting for turn/completed caused a 120s
    timeout regression when rich context was present (see #452).
    """
    connection = CodexConnection()
    connection._thread_id = "thread-1"
    connection._request = AsyncMock(return_value={"turn": {"id": "turn-1"}})  # type: ignore[method-assign]
    connection._wait_for_rollout_materialization = AsyncMock()  # type: ignore[method-assign]

    await connection._send_bootstrap_turn_and_wait(agent_name="gpt-dev")

    # Only rollout materialization should be awaited, not turn completion
    connection._wait_for_rollout_materialization.assert_awaited_once()
    assert not hasattr(connection, "_wait_for_turn_completion")
