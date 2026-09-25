"""Harness error classification for retry decisions."""

from enum import StrEnum


class ErrorCategory(StrEnum):
    RETRYABLE = "retryable"
    UNRECOVERABLE = "unrecoverable"
    TIMEOUT = "timeout"
    STRATEGY_CHANGE = "strategy_change"


_RETRYABLE_MARKERS: tuple[str, ...] = (
    "rate limit",
    "429",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "temporary failure",
    "connection reset",
    "connection refused",
    "network error",
    "econnreset",
    "econnrefused",
    "etimedout",
    "resource busy",
    "database is locked",
)

_UNRECOVERABLE_MARKERS: tuple[str, ...] = (
    "model not found",
    "unknown model",
    "unsupported model",
    "cannot be launched inside another claude code session",
    "nested sessions share runtime resources",
    "permission denied",
    "access denied",
    "forbidden",
    "unauthorized",
    "invalid api key",
    "no api key",
    "authentication failed",
    "token limit",
    "maximum tokens",
    "max tokens exceeded",
    "pi_prompt_rejected",
    "pi_rpc_no_response_after_initial_prompt",
    "pi_rpc_spawned_prompt_required",
    "opencode session endpoint did not become ready",
    "event stream liveness timeout",
)

_STRATEGY_CHANGE_MARKERS: tuple[str, ...] = (
    "context length",
    "context too long",
    "maximum context length",
    "prompt too long",
    "output too large",
    "response too large",
    "please reduce",
)


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _normalized_failure_text(stderr: str, failure_message: str | None) -> str:
    parts = [stderr]
    if failure_message:
        parts.append(failure_message)
    return "\n".join(parts).lower()


def classify_error(
    exit_code: int,
    stderr: str,
    timed_out: bool = False,
    failure_message: str | None = None,
) -> ErrorCategory:
    """Classify one failed harness attempt into a retry strategy category."""

    if timed_out:
        return ErrorCategory.TIMEOUT

    normalized = _normalized_failure_text(stderr, failure_message)

    # Context/output size issues need a different prompt strategy, not blind retries.
    if _contains_any(normalized, _STRATEGY_CHANGE_MARKERS):
        return ErrorCategory.STRATEGY_CHANGE
    if _contains_any(normalized, _UNRECOVERABLE_MARKERS):
        return ErrorCategory.UNRECOVERABLE
    if _contains_any(normalized, _RETRYABLE_MARKERS):
        return ErrorCategory.RETRYABLE

    if exit_code in {3}:
        return ErrorCategory.RETRYABLE
    if exit_code in {130, 143}:
        return ErrorCategory.UNRECOVERABLE
    if exit_code in {1, 2}:
        return ErrorCategory.RETRYABLE
    return ErrorCategory.UNRECOVERABLE


def should_retry(
    *,
    exit_code: int,
    stderr: str,
    timed_out: bool = False,
    failure_message: str | None = None,
    retries_attempted: int,
    max_retries: int = 3,
) -> bool:
    if timed_out:
        return False
    if retries_attempted >= max_retries:
        return False
    return (
        classify_error(
            exit_code,
            stderr,
            timed_out=timed_out,
            failure_message=failure_message,
        )
        == ErrorCategory.RETRYABLE
    )
