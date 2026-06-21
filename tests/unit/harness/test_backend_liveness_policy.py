"""Unit tests for BackendLivenessPolicy decision logic."""

from __future__ import annotations

import asyncio
import time

import pytest

from meridian.lib.harness.connections import liveness as liveness_module
from meridian.lib.harness.connections.liveness import (
    BackendLivenessPolicy,
    EventStreamLivenessTimeout,
    LivenessDecision,
)
from tests.support.async_determinism import AsyncDeterminism, assert_still_pending
from tests.support.fakes import FakeClock


def _policy(
    clock: FakeClock,
    *,
    pid: int | None = 4242,
    birth_time: float | None = 0.0,
    timeout_seconds: float = 10.0,
) -> BackendLivenessPolicy:
    return BackendLivenessPolicy(
        timeout_seconds=lambda: timeout_seconds,
        now=clock.monotonic,
        backend_pid=lambda: pid,
        backend_birth_time=lambda: birth_time,
    )


def test_evaluate_continue_when_stream_not_silent() -> None:
    clock = FakeClock(start=0.0)
    policy = _policy(clock)
    policy.mark_activity()

    assert policy.evaluate() == LivenessDecision.CONTINUE
    assert policy.healthy is True


def test_evaluate_suppress_when_turn_in_flight_and_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(start=0.0)
    policy = _policy(clock)
    policy.mark_activity()
    policy.signal_turn_started("turn-1")
    clock.advance(11.0)
    monkeypatch.setattr(liveness_module, "is_process_alive", lambda *_args, **_kwargs: True)

    assert policy.evaluate() == LivenessDecision.SUPPRESS
    assert policy.healthy is True


def test_evaluate_suppress_when_request_in_flight_and_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(start=0.0)
    policy = _policy(clock)
    policy.mark_activity()
    policy.signal_request_in_flight("rpc:1")
    clock.advance(11.0)
    monkeypatch.setattr(liveness_module, "is_process_alive", lambda *_args, **_kwargs: True)

    assert policy.evaluate() == LivenessDecision.SUPPRESS
    assert policy.healthy is True


def test_evaluate_stream_stalled_when_idle_silent_and_pid_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(start=0.0)
    policy = _policy(clock)
    policy.mark_activity()
    clock.advance(11.0)
    monkeypatch.setattr(liveness_module, "is_process_alive", lambda *_args, **_kwargs: True)

    assert policy.evaluate() == LivenessDecision.STREAM_STALLED
    assert policy.healthy is False


def test_evaluate_backend_dead_when_idle_silent_and_pid_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(start=0.0)
    policy = _policy(clock)
    policy.mark_activity()
    clock.advance(11.0)
    monkeypatch.setattr(liveness_module, "is_process_alive", lambda *_args, **_kwargs: False)

    assert policy.evaluate() == LivenessDecision.BACKEND_DEAD
    assert policy.healthy is False


def test_evaluate_backend_dead_when_silent_with_stale_active_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(start=0.0)
    policy = _policy(clock)
    policy.mark_activity()
    policy.signal_turn_started("turn-1")
    clock.advance(11.0)
    monkeypatch.setattr(liveness_module, "is_process_alive", lambda *_args, **_kwargs: False)

    assert policy.evaluate() == LivenessDecision.BACKEND_DEAD
    assert policy.healthy is False


def test_evaluate_backend_dead_when_pid_missing() -> None:
    clock = FakeClock(start=0.0)
    policy = _policy(clock, pid=None)
    policy.mark_activity()
    clock.advance(11.0)

    assert policy.evaluate() == LivenessDecision.BACKEND_DEAD


def test_evaluate_awaiting_done_suppresses_stream_stall_when_backend_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(start=0.0)
    policy = _policy(clock)
    policy.mark_activity()
    policy.set_awaiting_done(True)
    clock.advance(11.0)
    monkeypatch.setattr(liveness_module, "is_process_alive", lambda *_args, **_kwargs: True)

    assert policy.evaluate() == LivenessDecision.SUPPRESS
    assert policy.healthy is True


def test_close_classification_ignores_interleaved_awaiting_done_health_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(start=0.0)
    policy = _policy(clock)
    policy.mark_activity()
    clock.advance(11.0)
    monkeypatch.setattr(liveness_module, "is_process_alive", lambda *_args, **_kwargs: True)

    assert policy.classify_close_stream() == LivenessDecision.STREAM_STALLED

    policy.set_awaiting_done(True)
    assert policy.evaluate() == LivenessDecision.SUPPRESS
    assert policy.classify_close_stream() == LivenessDecision.STREAM_STALLED


def test_evaluate_awaiting_done_does_not_suppress_backend_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(start=0.0)
    policy = _policy(clock)
    policy.mark_activity()
    policy.set_awaiting_done(True)
    clock.advance(11.0)
    monkeypatch.setattr(liveness_module, "is_process_alive", lambda *_args, **_kwargs: False)

    assert policy.evaluate() == LivenessDecision.BACKEND_DEAD
    assert policy.healthy is False


@pytest.mark.asyncio
async def test_wait_for_activity_suppresses_during_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    determinism = AsyncDeterminism(start=0.0)
    determinism.install(monkeypatch)
    determinism.install_on_running_loop(monkeypatch)
    monkeypatch.setattr(liveness_module, "is_process_alive", lambda *_args, **_kwargs: True)
    policy = BackendLivenessPolicy(
        timeout_seconds=lambda: 0.05,
        now=determinism.clock.monotonic,
        backend_pid=lambda: 4242,
        backend_birth_time=lambda: 0.0,
    )
    policy.mark_activity()
    policy.signal_turn_started("turn-1")

    gate = asyncio.Event()

    async def blocked() -> str:
        await gate.wait()
        return "done"

    task = asyncio.create_task(policy.wait_for_activity(blocked()))
    await assert_still_pending(task)

    await determinism.sleep(0.1)
    await assert_still_pending(task)

    gate.set()
    assert await task == "done"


@pytest.mark.asyncio
async def test_wait_for_activity_raises_on_stream_stalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(liveness_module, "is_process_alive", lambda *_args, **_kwargs: True)
    policy = BackendLivenessPolicy(
        timeout_seconds=lambda: 0.05,
        now=time.monotonic,
        backend_pid=lambda: 4242,
        backend_birth_time=lambda: 0.0,
    )
    policy.mark_activity()

    gate = asyncio.Event()

    async def blocked() -> None:
        await gate.wait()

    with pytest.raises(EventStreamLivenessTimeout):
        await policy.wait_for_activity(blocked())


@pytest.mark.asyncio
async def test_wait_for_activity_resolves_after_multiple_suppress_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    determinism = AsyncDeterminism(start=0.0)
    determinism.install(monkeypatch)
    determinism.install_on_running_loop(monkeypatch)
    monkeypatch.setattr(liveness_module, "is_process_alive", lambda *_args, **_kwargs: True)
    policy = BackendLivenessPolicy(
        timeout_seconds=lambda: 0.05,
        now=determinism.clock.monotonic,
        backend_pid=lambda: 4242,
        backend_birth_time=lambda: 0.0,
    )
    policy.mark_activity()
    policy.signal_turn_started("turn-1")

    gate = asyncio.Event()

    async def blocked() -> str:
        await determinism.sleep(0.15)
        gate.set()
        return "done"

    task = asyncio.create_task(policy.wait_for_activity(blocked()))
    await gate.wait()
    assert await task == "done"


@pytest.mark.asyncio
async def test_wait_for_activity_raises_on_backend_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(liveness_module, "is_process_alive", lambda *_args, **_kwargs: False)
    policy = BackendLivenessPolicy(
        timeout_seconds=lambda: 0.05,
        now=time.monotonic,
        backend_pid=lambda: 4242,
        backend_birth_time=lambda: 0.0,
    )
    policy.mark_activity()

    gate = asyncio.Event()

    async def blocked() -> None:
        await gate.wait()

    with pytest.raises(EventStreamLivenessTimeout):
        await policy.wait_for_activity(blocked())
