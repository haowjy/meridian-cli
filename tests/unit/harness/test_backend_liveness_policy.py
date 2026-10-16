"""Unit tests for BackendLivenessPolicy decision logic."""

from __future__ import annotations

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
    ("signal", "elapsed", "pid", "pid_alive", "expected", "healthy"),
    [
        pytest.param(None, 0.0, 4242, True, LivenessDecision.CONTINUE, True, id="active"),
        pytest.param("turn", 11.0, 4242, True, LivenessDecision.SUPPRESS, True, id="turn"),
        pytest.param("request", 11.0, 4242, True, LivenessDecision.SUPPRESS, True, id="request"),
        pytest.param(None, 11.0, 4242, True, LivenessDecision.STREAM_STALLED, False, id="stalled"),
        pytest.param(None, 11.0, 4242, False, LivenessDecision.BACKEND_DEAD, False, id="dead"),
        pytest.param(
            "turn", 11.0, 4242, False, LivenessDecision.BACKEND_DEAD, False, id="stale-turn"
        ),
        pytest.param(
            None, 11.0, None, True, LivenessDecision.BACKEND_DEAD, None, id="missing-pid"
        ),
        pytest.param("awaiting", 11.0, 4242, True, LivenessDecision.SUPPRESS, True, id="awaiting"),
        pytest.param(
            "awaiting",
            11.0,
            4242,
            False,
            LivenessDecision.BACKEND_DEAD,
            False,
            id="awaiting-dead",
        ),
    ],
)
def test_evaluate_liveness_policy(
    monkeypatch: pytest.MonkeyPatch,
    signal: str | None,
    elapsed: float,
    pid: int | None,
    pid_alive: bool,
    expected: LivenessDecision,
    healthy: bool | None,
) -> None:
    clock = FakeClock(start=0.0)
    policy = _policy(clock, pid=pid)
    policy.mark_activity()
    if signal == "turn":
        policy.signal_turn_started("turn-1")
    elif signal == "request":
        policy.signal_request_in_flight("rpc:1")
    elif signal == "awaiting":
        policy.set_awaiting_done(True)
    clock.advance(elapsed)
    monkeypatch.setattr(
        liveness_module,
        "is_process_alive",
        lambda *_args, **_kwargs: pid_alive,
    )

    assert policy.evaluate() == expected
    if healthy is not None:
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
