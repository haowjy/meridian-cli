from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.launch.streaming_runner import _sleep_retry_backoff_or_cancel
from meridian.lib.state import spawn_store


@pytest.mark.asyncio
async def test_retry_backoff_wakes_when_cancel_intent_is_recorded(tmp_path: Path) -> None:
    spawn_id = SpawnId("p1")
    spawn_store.start_spawn(
        tmp_path,
        spawn_id=spawn_id,
        chat_id="c1",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="hello",
    )
    shutdown_event = asyncio.Event()

    async def request_cancel() -> None:
        await asyncio.sleep(0.01)
        spawn_store.record_cancel_intent(
            tmp_path,
            spawn_id,
            exit_code=130,
            error="cancelled",
            requested_at="2026-06-03T01:00:00Z",
        )

    task = asyncio.create_task(request_cancel())
    try:
        cancelled = await _sleep_retry_backoff_or_cancel(
            delay_seconds=10.0,
            shutdown_event=shutdown_event,
            runtime_root=tmp_path,
            spawn_id=spawn_id,
        )
    finally:
        await task

    assert cancelled is True
