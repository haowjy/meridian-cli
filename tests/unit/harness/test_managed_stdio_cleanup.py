"""Typed reduction of process-scope cleanup facts."""

import pytest

from meridian.lib.harness.connections.errors import TeardownStatus
from meridian.lib.harness.connections.managed_stdio import teardown_from_scope_cleanup
from meridian.lib.platform.process_scope import CleanupResult


def _result(
    *,
    skip_reason: str | None = None,
    survivor_count: int | None = None,
    verification_complete: bool = False,
) -> CleanupResult:
    return CleanupResult(
        scope_id="stdio",
        root_pid=123,
        descendant_count=None,
        reason="test",
        grace_seconds=0.1,
        kill_escalated=False,
        degraded_fallback=False,
        skip_reason=skip_reason,
        survivor_count=survivor_count,
        verification_complete=verification_complete,
    )


@pytest.mark.parametrize("skip_reason", ["termination_exception", "pid_reuse_detected"])
def test_skipped_scope_termination_is_failed(skip_reason: str) -> None:
    assert (
        teardown_from_scope_cleanup(_result(skip_reason=skip_reason))
        is TeardownStatus.FAILED
    )


def test_observed_survivor_is_failed() -> None:
    assert (
        teardown_from_scope_cleanup(
            _result(survivor_count=1, verification_complete=True)
        )
        is TeardownStatus.FAILED
    )


def test_incomplete_scope_verification_is_unknown() -> None:
    assert (
        teardown_from_scope_cleanup(
            _result(survivor_count=0, verification_complete=False)
        )
        is TeardownStatus.UNKNOWN
    )


def test_verified_empty_scope_is_quiescent() -> None:
    assert (
        teardown_from_scope_cleanup(
            _result(survivor_count=0, verification_complete=True)
        )
        is TeardownStatus.QUIESCENT
    )
