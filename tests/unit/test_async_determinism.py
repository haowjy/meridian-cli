"""Regression tests for deterministic async test helpers."""

from __future__ import annotations

import asyncio
import time

import pytest

from tests.support.async_determinism import wait_until


@pytest.mark.asyncio
async def test_wait_until_timeout_uses_real_clock_when_stdlib_clock_is_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_monotonic = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)

    started = real_monotonic()
    with pytest.raises(AssertionError, match="timed out waiting for never true"):
        await asyncio.wait_for(
            wait_until(lambda: False, timeout=0.01, description="never true"),
            timeout=1.0,
        )

    assert real_monotonic() - started < 6.0
