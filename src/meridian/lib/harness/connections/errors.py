"""Typed connection-start failures and initial-turn progress."""

from enum import StrEnum


class TurnSubmission(StrEnum):
    """What a transport proves about delivery of the initial user turn."""

    NOT_SUBMITTED = "not_submitted"
    SUBMITTED = "submitted"
    UNKNOWN = "unknown"


class ConnectionStartupError(RuntimeError):
    """Base class for connection startup failures."""


class RetryableConnectionStartupError(ConnectionStartupError):
    """Startup failure that can be retried with new config."""


class PortBindError(RetryableConnectionStartupError):
    """Backend failed to bind pre-reserved loopback port (TOCTOU race)."""


class ConnectionStartFailure(RuntimeError):
    """A start exception preserved with the connection's final progress facts."""

    def __init__(
        self,
        cause: Exception,
        *,
        turn_submission: TurnSubmission,
        subprocess_pid: int | None,
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.turn_submission = turn_submission
        self.subprocess_pid = subprocess_pid


__all__ = [
    "ConnectionStartFailure",
    "ConnectionStartupError",
    "PortBindError",
    "RetryableConnectionStartupError",
    "TurnSubmission",
]
