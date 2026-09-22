"""Concurrency contracts for bounded descendant refresh ownership."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.streaming.descendant_evidence import (
    DescendantRefreshOwner,
    ReconciledDescendantEvidence,
)


@pytest.mark.asyncio
async def test_held_refresh_keeps_cached_reads_immediate_and_coalesces_requests(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    reads = 0

    def held_reader():  # type: ignore[no-untyped-def]
        nonlocal reads
        reads += 1
        entered.set()
        release.wait(timeout=5)
        return ()

    owner = DescendantRefreshOwner(
        ReconciledDescendantEvidence(
            runtime_root=tmp_path,
            root_spawn_id=SpawnId("p1"),
            blocker_reader=held_reader,
        ),
        poll_seconds=0.25,
        clock=lambda: 100.0,
    )
    owner.start()
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(3):
            assert owner.assessment.disposition == "unknown"
        first = owner.request()
        second = owner.request()
        assert second > first
        assert reads == 1
        release.set()
        while not owner.validated(second):
            await asyncio.wait_for(owner.wait(), timeout=2)
        assert reads == 2
    finally:
        release.set()
        await owner.stop()
