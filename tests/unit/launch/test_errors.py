from __future__ import annotations

from meridian.lib.launch.errors import should_retry


def test_should_not_retry_opencode_liveness_startup_failure() -> None:
    assert not should_retry(
        exit_code=2,
        stderr="",
        failure_message="OpenCode session endpoint did not become ready within 120.0s",
        retries_attempted=0,
        max_retries=3,
    )


def test_should_not_retry_event_stream_liveness_timeout() -> None:
    assert not should_retry(
        exit_code=1,
        stderr="",
        failure_message="OpenCode event stream liveness timeout after 120.0s without events",
        retries_attempted=0,
        max_retries=3,
    )
