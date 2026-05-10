"""Unit tests for capture_library_diagnostics — pure logging behavior, no filesystem I/O.

The filesystem-backed inventory prompt regression lives in
tests/integration/catalog/test_diagnostics.py.

# qa-validated: test-suite-redesign
"""

import io
import logging

from meridian.lib.diagnostics import capture_library_diagnostics


def _root_stream_handler() -> tuple[logging.StreamHandler[io.StringIO], io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.WARNING)
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.WARNING)
    return handler, stream


def test_capture_library_diagnostics_captures_warnings_not_stderr() -> None:
    handler, stream = _root_stream_handler()
    logger = logging.getLogger("meridian.lib.catalog.agent")
    try:
        with capture_library_diagnostics() as diag:
            logger.warning("catalog warning")

        assert [record.getMessage() for record in diag.records] == ["catalog warning"]
        assert stream.getvalue() == ""
    finally:
        logging.getLogger().removeHandler(handler)


def test_capture_library_diagnostics_allows_errors_to_stderr() -> None:
    handler, stream = _root_stream_handler()
    logger = logging.getLogger("meridian.lib.catalog.agent")
    try:
        with capture_library_diagnostics() as diag:
            logger.error("catalog error")

        assert diag.records == []
        assert "catalog error" in stream.getvalue()
    finally:
        logging.getLogger().removeHandler(handler)


def test_capture_library_diagnostics_leaves_non_meridian_warnings_unaffected() -> None:
    handler, stream = _root_stream_handler()
    logger = logging.getLogger("external.library")
    try:
        with capture_library_diagnostics() as diag:
            logger.warning("external warning")

        assert diag.records == []
        assert "external warning" in stream.getvalue()
    finally:
        logging.getLogger().removeHandler(handler)


def test_capture_library_diagnostics_restores_warning_logging_after_exit() -> None:
    handler, stream = _root_stream_handler()
    logger = logging.getLogger("meridian.lib.catalog.agent")
    try:
        with capture_library_diagnostics():
            logger.warning("captured warning")

        logger.warning("normal warning")

        output = stream.getvalue()
        assert "captured warning" not in output
        assert "normal warning" in output
    finally:
        logging.getLogger().removeHandler(handler)
