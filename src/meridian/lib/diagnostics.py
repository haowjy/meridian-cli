from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager


class _DiagnosticCapture(logging.Handler):
    """Capture WARNING-level records from ``meridian.lib.*`` loggers."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("meridian.lib") and record.levelno < logging.ERROR:
            self.records.append(record)


class _SuppressLibraryWarnings(logging.Filter):
    """Suppress library diagnostics below ERROR on installed handlers."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.name.startswith("meridian.lib") and record.levelno < logging.ERROR)


@contextmanager
def capture_library_diagnostics() -> Generator[_DiagnosticCapture, None, None]:
    """Capture ``meridian.lib`` warnings without emitting them to stderr.

    ERROR and above still flow through existing logging handlers. The filter is
    installed only on handlers that exist at context entry, which matches the
    launch boundary use case where CLI logging is configured before launch.
    """

    lib_logger = logging.getLogger("meridian.lib")
    root_logger = logging.getLogger()
    capture = _DiagnosticCapture()
    suppress_filter = _SuppressLibraryWarnings()
    filtered_handlers = (*root_logger.handlers, *lib_logger.handlers)

    for handler in filtered_handlers:
        handler.addFilter(suppress_filter)
    lib_logger.addHandler(capture)

    try:
        yield capture
    finally:
        lib_logger.removeHandler(capture)
        for handler in filtered_handlers:
            handler.removeFilter(suppress_filter)


__all__ = ["capture_library_diagnostics"]
