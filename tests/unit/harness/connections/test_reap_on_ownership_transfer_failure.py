"""Unit tests for bounded ownership-transfer cleanup."""

from __future__ import annotations

import asyncio
import time

import pytest

from meridian.lib.harness.connections.base import reap_on_ownership_transfer_failure


@pytest.mark.asyncio
async def test_reap_survives_repeated_cancellation_until_cleanup_finishes() -> None:
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def cleanup() -> None:
        cleanup_started.set()
        await release_cleanup.wait()
        cleanup_finished.set()

    reap_task = asyncio.create_task(reap_on_ownership_transfer_failure(cleanup))

    await cleanup_started.wait()
    reap_task.cancel()
    await asyncio.sleep(0)
    assert not reap_task.done()
    assert not cleanup_finished.is_set()

    reap_task.cancel()
    await asyncio.sleep(0)
    assert not reap_task.done()

    release_cleanup.set()
    await reap_task

    assert cleanup_finished.is_set()


@pytest.mark.asyncio
async def test_reap_returns_when_cleanup_never_finishes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cleanup_started = asyncio.Event()

    async def cleanup() -> None:
        cleanup_started.set()
        await asyncio.Event().wait()

    start = time.monotonic()
    reap_task = asyncio.create_task(
        reap_on_ownership_transfer_failure(cleanup, deadline_seconds=0.05)
    )

    await cleanup_started.wait()
    await reap_task
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    assert any(
        "Abandoning foreground ownership-transfer cleanup" in record.message
        for record in caplog.records
    )
