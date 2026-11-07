from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

# Hardcoded blocklist for secret-like keys (case-insensitive).
SECRET_BLOCKLIST = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "private_key",
        "access_token",
        "refresh_token",
        "bearer",
        "key",
        "secret_key",
    }
)

MAX_STRING_LENGTH = 512
TRUNCATION_SUFFIX = "...[truncated]"


@dataclass(frozen=True)
class ExtensionInvocationSummary:
    """Summary of one extension command invocation."""

    fqid: str
    caller_surface: str
    request_id: str | None
    started_at: str
    duration_ms: float
    success: bool
    error_code: str | None = None
    args_redacted: dict[str, Any] = field(default_factory=dict[str, Any])
    result_redacted: dict[str, Any] = field(default_factory=dict[str, Any])


class RedactionPipeline:
    """Redact sensitive data from extension invocation dictionaries."""

    @classmethod
    def redact(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Redact a dictionary recursively.

        EB1.11: Drops hard-blocklist keys case-insensitively.
        Also truncates strings longer than 512 bytes.
        """

        return cls._redact_dict(data)

    @classmethod
    def _redact_dict(cls, data: dict[str, Any]) -> dict[str, Any]:
        redacted: dict[str, Any] = {}
        for key, value in data.items():
            if key.lower() in SECRET_BLOCKLIST:
                continue
            redacted[key] = cls._redact_value(value)
        return redacted

    @classmethod
    def _redact_value(cls, value: Any) -> Any:
        if isinstance(value, str):
            return cls._truncate_string(value)
        if isinstance(value, dict):
            return cls._redact_dict(cast("dict[str, Any]", value))
        if isinstance(value, list):
            return [cls._redact_value(item) for item in cast("list[Any]", value)]
        return value

    @classmethod
    def _truncate_string(cls, value: str) -> str:
        encoded = value.encode("utf-8", errors="replace")
        if len(encoded) <= MAX_STRING_LENGTH:
            return value
        truncated = encoded[:MAX_STRING_LENGTH].decode("utf-8", errors="ignore")
        return f"{truncated}{TRUNCATION_SUFFIX}"


class ObservabilityWriter:
    """Append invocation summaries to JSONL files.

    EB1.10: Log write failures to stderr and never raise.
    """

    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path

    def write_summary(self, summary: ExtensionInvocationSummary) -> None:
        """Write one summary line; never raises."""

        try:
            from meridian.lib.state.atomic import append_text_line

            payload = {
                "fqid": summary.fqid,
                "caller_surface": summary.caller_surface,
                "request_id": summary.request_id,
                "started_at": summary.started_at,
                "duration_ms": summary.duration_ms,
                "success": summary.success,
                "error_code": summary.error_code,
                "args_redacted": summary.args_redacted,
                "result_redacted": summary.result_redacted,
            }
            append_text_line(self._log_path, json.dumps(payload, default=str))
        except Exception as error:  # pragma: no cover - defensive path.
            print(f"[observability] Failed to write summary: {error}", file=sys.stderr)


class InvocationTimer:
    """Tracks start timestamp and elapsed invocation duration."""

    def __init__(self) -> None:
        self._start_time = time.perf_counter()
        self._started_at = datetime.now(UTC).isoformat()

    @property
    def started_at(self) -> str:
        return self._started_at

    @property
    def duration_ms(self) -> float:
        return (time.perf_counter() - self._start_time) * 1000


__all__ = [
    "MAX_STRING_LENGTH",
    "SECRET_BLOCKLIST",
    "ExtensionInvocationSummary",
    "InvocationTimer",
    "ObservabilityWriter",
    "RedactionPipeline",
]
