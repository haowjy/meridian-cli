"""Process-local telemetry router with bounded non-blocking queue."""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Sequence
from contextlib import suppress
from typing import Any

from meridian.lib.telemetry.events import (
    TelemetryEnvelope,
    make_error_data,
    utc_timestamp,
    validate_event,
)
from meridian.lib.telemetry.sinks import NoopSink, TelemetrySink

logger = logging.getLogger(__name__)

_DEFAULT_MAX_QUEUE = 10_000
_DEFAULT_FLUSH_INTERVAL_SECS = 1.0
_DEFAULT_BATCH_SIZE = 100


class TelemetryRouter:
    """Non-blocking telemetry producer route to a background sink writer."""

    def __init__(
        self,
        sink: TelemetrySink | None = None,
        *,
        max_queue: int = _DEFAULT_MAX_QUEUE,
        flush_interval_secs: float = _DEFAULT_FLUSH_INTERVAL_SECS,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> None:
        self._sink: TelemetrySink | None = sink or NoopSink()
        self._queue: deque[TelemetryEnvelope] = deque()
        self._max_queue = max_queue
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = False
        self._dropped = 0
        self._sink_failed_reported = False
        self._flush_interval_secs = flush_interval_secs
        self._batch_size = batch_size
        self._thread = threading.Thread(
            target=self._writer_loop,
            name="meridian-telemetry-writer",
            daemon=True,
        )
        self._thread.start()

    def emit(
        self,
        domain: str,
        event: str,
        *,
        scope: str,
        ids: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
        severity: str = "info",
    ) -> None:
        """Construct and enqueue one envelope. Never raise to callers."""
        try:
            validate_event(domain, event, severity)
            envelope = TelemetryEnvelope(
                v=1,
                ts=utc_timestamp(),
                domain=domain,
                event=event,
                scope=scope,
                severity=severity,
                ids=ids,
                data=data,
            )
            self.enqueue(envelope)
        except Exception:
            logger.debug("Telemetry emit failed", exc_info=True)

    def enqueue(self, envelope: TelemetryEnvelope) -> None:
        """Enqueue an already-constructed envelope without blocking."""
        with self._lock:
            if self._closed:
                return
            if len(self._queue) >= self._max_queue:
                self._dropped += 1
                return
            self._queue.append(envelope)
        self._wake.set()

    def close(self) -> None:
        """Flush pending events and close the sink."""
        with self._lock:
            self._closed = True
        self._wake.set()
        self._thread.join(timeout=2.0)
        sink = self._sink
        if sink is not None:
            try:
                sink.close()
            except Exception:
                logger.debug("Telemetry sink close failed", exc_info=True)

    def _writer_loop(self) -> None:
        while True:
            self._wake.wait(self._flush_interval_secs)
            self._wake.clear()
            batch = self._drain_batch()
            if batch:
                self._write_batch(batch)
            with self._lock:
                should_stop = self._closed and not self._queue
            if should_stop:
                break

    def _drain_batch(self) -> list[TelemetryEnvelope]:
        with self._lock:
            batch: list[TelemetryEnvelope] = []
            if self._dropped:
                dropped = self._dropped
                self._dropped = 0
                error_data = make_error_data(
                    message=f"{dropped} telemetry event(s) dropped due to queue overflow"
                )
                error_data["error"]["type"] = "QueueOverflow"
                batch.append(
                    TelemetryEnvelope(
                        v=1,
                        ts=utc_timestamp(),
                        domain="runtime",
                        event="runtime.telemetry.dropped",
                        scope="telemetry.router",
                        severity="warning",
                        data={**error_data, "count": dropped},
                    )
                )
            while self._queue and len(batch) < self._batch_size:
                batch.append(self._queue.popleft())
            return batch

    def _write_batch(self, events: Sequence[TelemetryEnvelope]) -> None:
        sink = self._sink
        if sink is None:
            return
        try:
            sink.write_batch(events)
        except Exception as exc:
            logger.exception("Telemetry sink failed")
            self._sink = None
            if not self._sink_failed_reported:
                self._sink_failed_reported = True
                _ = exc
                logger.error("runtime.telemetry.sink_failed")


_global_router_lock = threading.Lock()
_global_router: TelemetryRouter | None = None


def set_global_router(router: TelemetryRouter) -> None:
    """Replace the process-global router."""
    global _global_router
    with _global_router_lock:
        old = _global_router
        _global_router = router
    if old is not None:
        old.close()


def get_global_router() -> TelemetryRouter:
    """Return the process-global router, creating a noop router lazily."""
    global _global_router
    with _global_router_lock:
        if _global_router is None:
            _global_router = TelemetryRouter(NoopSink())
        return _global_router


def emit_telemetry(
    domain: str,
    event: str,
    *,
    scope: str,
    ids: dict[str, str] | None = None,
    data: dict[str, Any] | None = None,
    severity: str = "info",
) -> None:
    """Emit through the process-global router. Never raises."""
    with suppress(Exception):
        get_global_router().emit(
            domain,
            event,
            scope=scope,
            ids=ids,
            data=data,
            severity=severity,
        )
