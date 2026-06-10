from __future__ import annotations

import pytest

from meridian.lib.launch.errors import ErrorCategory, classify_error, should_retry


@pytest.mark.parametrize(
    "marker",
    (
        "opencode session endpoint did not become ready",
        "event stream liveness timeout",
    ),
)
def test_liveness_failures_are_unrecoverable(marker: str) -> None:
    assert classify_error(
        exit_code=1,
        stderr="",
        failure_message=f"prefix {marker} suffix",
    ) == ErrorCategory.UNRECOVERABLE
    assert not should_retry(
        exit_code=1,
        stderr="",
        failure_message=f"prefix {marker} suffix",
        retries_attempted=0,
        max_retries=3,
    )
