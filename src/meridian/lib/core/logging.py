"""Structlog configuration helpers."""

import logging as std_logging
import sys

import structlog


def _level_from_verbosity(verbosity: int) -> int:
    if verbosity <= 0:
        return std_logging.WARNING
    if verbosity == 1:
        return std_logging.INFO
    return std_logging.DEBUG


def configure_logging(json_mode: bool = False, verbosity: int = 0) -> None:
    """Configure structlog for CLI or MCP server mode."""

    level = _level_from_verbosity(verbosity)
    # Route log output to stderr so it never pollutes JSON/structured stdout.
    handler = std_logging.StreamHandler(sys.stderr)
    handler.setFormatter(std_logging.Formatter("%(message)s"))
    std_logging.basicConfig(level=level, handlers=[handler])

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json_mode else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # Route structlog output to stderr so it never pollutes JSON stdout.
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
