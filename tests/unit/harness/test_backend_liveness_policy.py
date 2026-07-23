"""Unit tests for BackendLivenessPolicy decision logic."""

from __future__ import annotations

from typing import TYPE_CHECKING

from meridian.lib.harness.connections import liveness as liveness_module
from meridian.lib.harness.connections.liveness import (
    BackendLivenessPolicy,
    LivenessDecision,
)
from tests.support.fakes import FakeClock

if TYPE_CHECKING:
    import pytest


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
