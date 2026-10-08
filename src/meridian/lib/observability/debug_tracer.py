"""Structured JSONL debug event writer for streaming pipeline observability."""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)


class DebugTracer:
    """Structured JSONL debug event writer for streaming pipeline observability.

    Contract: emit() is best-effort and never raises. If the underlying file
    write or serialization fails, the tracer logs one warning and disables
    itself for the remainder of the session.
    """

    def __init__(
        self,
        spawn_id: str,
        debug_path: Path,
        *,
        echo_stderr: bool = False,
        max_payload_bytes: int = 4096,
        report_failures: bool = True,
    ) -> None:
        self._spawn_id = spawn_id
        self._debug_path = debug_path
        self._echo_stderr = echo_stderr
        self._max_payload_bytes = max_payload_bytes
        self._report_failures = report_failures
        self._lock = threading.Lock()
        self._handle: IO[str] | None = None
        self._disabled = False

    def emit(
        self,
        layer: str,
        event: str,
        *,
        direction: str = "internal",
        data: dict[str, object] | None = None,
    ) -> None:
        """Append one structured debug event. Never raises.

        If the underlying write fails, logs a warning on the first failure,
        sets self._disabled = True, and returns silently on all subsequent calls.
        """
        if self._disabled:
            return

        try:
            record: dict[str, object] = {
                "ts": time.time(),
                "spawn_id": self._spawn_id,
                "layer": layer,
                "direction": direction,
                "event": event,
            }
            if data is not None:
                record["data"] = self._prepare_data(data)

            line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"

            with self._lock:
                self._ensure_open()
                handle = self._handle
                if handle is not None:
                    handle.write(line)
                    handle.flush()

            if self._echo_stderr:
                sys.stderr.write(line)
                sys.stderr.flush()
        except Exception as exc:
            self._disabled = True
            if not self._report_failures:
                return
            from meridian.lib.telemetry import emit_telemetry
            from meridian.lib.telemetry.events import make_error_data

            emit_telemetry(
                "runtime",
                "runtime.debug_tracer_disabled",
                scope="observability.debug_tracer",
                severity="warning",
                ids={"spawn_id": self._spawn_id},
                data=make_error_data(exc=exc),
            )
            logger.warning(
                "Debug tracer disabled after write failure for spawn %s",
                self._spawn_id,
                exc_info=True,
            )

    def close(self) -> None:
        """Flush and close the debug file handle. Idempotent."""
        with self._lock:
            handle = self._handle
            if handle is not None:
                try:
                    handle.flush()
                    handle.close()
                except Exception:
                    pass
                self._handle = None

    def _ensure_open(self) -> None:
        """Open the file handle on first write. Creates parent dirs."""
        if self._handle is not None:
            return
        self._debug_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._debug_path.open("a", encoding="utf-8")

    def _prepare_data(self, data: dict[str, object]) -> dict[str, object]:
        """Serialize and truncate data values for JSONL output."""
        result: dict[str, object] = {}
        for key, value in data.items():
            if isinstance(value, str):
                result[key] = self._truncate(value)
            elif isinstance(value, dict | list):
                try:
                    serialized = json.dumps(value, ensure_ascii=False)
                except (TypeError, ValueError):
                    serialized = repr(value)  # type: ignore[arg-type]
                result[key] = self._truncate(serialized)
            else:
                result[key] = value  # int, float, bool, None — pass through
        return result

    def _truncate(self, value: str) -> str:
        if len(value) <= self._max_payload_bytes:
            return value
        return value[: self._max_payload_bytes] + f"...[truncated, {len(value)}B total]"


__all__ = ["DebugTracer"]
