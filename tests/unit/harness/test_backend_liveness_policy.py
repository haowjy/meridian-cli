"""Unit tests for BackendLivenessPolicy decision logic."""

from __future__ import annotations

import asyncio
import time

import pytest

from meridian.lib.harness.connections import liveness as liveness_module
from meridian.lib.harness.connections.liveness import (
    BackendLivenessPolicy,
    LivenessDecision,
)
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


@pytest.mark.parametrize(
    ("setup", "pid", "silent", "alive", "expected", "healthy"),
    [
        pytest.param(None, 4242, False, None, LivenessDecision.CONTINUE, True, id="active"),
        pytest.param("turn", 4242, True, True, LivenessDecision.SUPPRESS, True, id="turn"),
        pytest.param("request", 4242, True, True, LivenessDecision.SUPPRESS, True, id="request"),
        pytest.param(
            None, 4242, True, True, LivenessDecision.STREAM_STALLED, False, id="stalled"
        ),
        pytest.param(None, 4242, True, False, LivenessDecision.BACKEND_DEAD, False, id="dead"),
        pytest.param(
            "turn", 4242, True, False, LivenessDecision.BACKEND_DEAD, False, id="dead-stale-turn"
        ),
        pytest.param(None, None, True, None, LivenessDecision.BACKEND_DEAD, False, id="no-pid"),
        pytest.param(
            "awaiting-done",
            4242,
            True,
            True,
            LivenessDecision.SUPPRESS,
            True,
            id="awaiting-done-alive",
        ),
        pytest.param(
            "awaiting-done",
            4242,
            True,
            False,
            LivenessDecision.BACKEND_DEAD,
            False,
            id="awaiting-done-dead",
        ),
    ],
)
def test_evaluate_decides_liveness(
    setup: str | None,
    pid: int | None,
    silent: bool,
    alive: bool | None,
    expected: LivenessDecision,
    healthy: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(start=0.0)
    policy = _policy(clock, pid=pid)
    policy.mark_activity()
    if setup == "turn":
        policy.signal_turn_started("turn-1")
    elif setup == "request":
        policy.signal_request_in_flight("rpc:1")
    elif setup == "awaiting-done":
        policy.set_awaiting_done(True)
    if silent:
        clock.advance(11.0)
    if alive is not None:
        monkeypatch.setattr(liveness_module, "is_process_alive", lambda *_args, **_kwargs: alive)

    assert policy.evaluate() == expected
    assert policy.healthy is healthy


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


async def test_wait_for_activity_returns_read_completed_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Data that arrived at the deadline is progress, not a stalled stream."""

    clock = FakeClock(start=0.0)
    policy = _policy(clock)
    policy.mark_activity()
    clock.advance(11.0)
    monkeypatch.setattr(liveness_module, "is_process_alive", lambda *_args, **_kwargs: True)

    read = asyncio.get_running_loop().create_future()
    read.set_result(b"chunk")

    assert await policy.wait_for_activity(read) == b"chunk"


async def test_wait_for_activity_propagates_outer_cancel_during_inner_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outer cancel is not turned into a liveness timeout.

    After the deadline the policy cancels the read and awaits it. Codex and
    OpenCode both rely on an outer cancel (shutdown) landing in that window as
    ``CancelledError``; reporting ``EventStreamLivenessTimeout`` there would look
    like a stream stall.
    """

    # Real time: the read must already be running when the deadline cancels it,
    # and a completed-at-the-deadline read is covered above.
    policy = BackendLivenessPolicy(
        timeout_seconds=lambda: 0.05,
        now=time.monotonic,
        backend_pid=lambda: 4242,
        backend_birth_time=lambda: 0.0,
    )
    monkeypatch.setattr(liveness_module, "is_process_alive", lambda *_args, **_kwargs: True)
    policy.mark_activity()

    cleanup_started = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def slow_read() -> bytes:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            await cleanup_finished.wait()
            raise

    waiter = asyncio.ensure_future(policy.wait_for_activity(slow_read()))
    await asyncio.wait_for(cleanup_started.wait(), timeout=5.0)
    waiter.cancel()
    cleanup_finished.set()

    with pytest.raises(asyncio.CancelledError):
        await waiter


def test_record_probe_restarts_the_window_without_counting_activity() -> None:
    """A busy verdict keeps the stream open but is not a semantic event."""

    clock = FakeClock(start=0.0)
    policy = _policy(clock)
    policy.mark_activity()
    clock.advance(11.0)

    assert policy.record_probe(True) is True
    assert policy.evaluate() == LivenessDecision.CONTINUE
    # The busy bound still measures the real silence, not the probe.
    assert policy.silence_seconds() == pytest.approx(11.0)


def test_record_probe_none_carries_a_running_verdict_once() -> None:
    clock = FakeClock(start=0.0)
    policy = _policy(clock)
    policy.mark_activity()
    clock.advance(11.0)
    assert policy.record_probe(True) is True

    clock.advance(8.0)
    assert policy.record_probe(None) is True
    # The carried miss restarts the window: 8s later it is still inside it, which
    # it would not be had the window run on from the vouching probe (16s > 10s).
    clock.advance(8.0)
    assert policy.evaluate() == LivenessDecision.CONTINUE
    assert policy.record_probe(None) is False


def test_record_probe_none_outside_a_running_stretch_is_not_running() -> None:
    clock = FakeClock(start=0.0)
    policy = _policy(clock)
    policy.mark_activity()
    clock.advance(11.0)

    assert policy.record_probe(None) is False


def test_mark_activity_ends_the_running_verdict_it_objected_to() -> None:
    """The bug class: a stale verdict must not outlive the event that ends it."""

    clock = FakeClock(start=0.0)
    policy = _policy(clock)
    policy.mark_activity()
    clock.advance(11.0)
    assert policy.record_probe(True) is True

    policy.mark_activity()
    clock.advance(11.0)

    assert policy.record_probe(None) is False
    assert policy.silence_seconds() == pytest.approx(11.0)


def test_record_probe_false_clears_the_repeated_miss_budget() -> None:
    clock = FakeClock(start=0.0)
    policy = _policy(clock)
    policy.mark_activity()
    assert policy.record_probe(True) is True
    assert policy.record_probe(None) is True

    assert policy.record_probe(False) is False
    assert policy.record_probe(None) is False


def test_record_probe_true_after_a_carried_miss_restores_the_budget() -> None:
    clock = FakeClock(start=0.0)
    policy = _policy(clock)
    policy.mark_activity()

    assert policy.record_probe(True) is True
    assert policy.record_probe(None) is True
    assert policy.record_probe(True) is True
    assert policy.record_probe(None) is True
