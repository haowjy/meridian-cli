"""Pure terminal-outcome priority coverage for streaming drain."""

from meridian.lib.streaming.spawn_drain_loop import resolve_terminal_outcome
from meridian.lib.streaming.spawn_session import DrainOutcome


def test_terminal_outcome_priority_is_success_then_stop_then_drain() -> None:
    succeeded = DrainOutcome(status="succeeded", exit_code=0)
    stopped = DrainOutcome(status="timed_out", exit_code=3, error="timeout")
    failed = DrainOutcome(status="failed", exit_code=1, error="drain failed")

    assert resolve_terminal_outcome(succeeded, stopped) is succeeded
    assert resolve_terminal_outcome(failed, stopped) is stopped
    assert resolve_terminal_outcome(failed, None) is failed
