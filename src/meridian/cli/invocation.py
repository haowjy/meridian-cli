"""Per-invocation CLI options and output routing."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Never, Protocol

from pydantic import BaseModel, ConfigDict

from meridian.cli.mode import RenderMode, is_agent_render_mode
from meridian.cli.output import (
    CLIOutputProtocol,
    OutputConfig,
    OutputFormat,
    create_sink,
    flush_sink,
)
from meridian.cli.output import emit as emit_output
from meridian.lib.core.logging import warning_cache_scope
from meridian.lib.core.sink import OutputSink
from meridian.lib.core.util import FormatContext


class Caller(BaseModel):
    """The calling Meridian session, read from the env before ``-C`` can drop it.

    Under a cross-project ``-C`` the env no longer carries the caller's IDs, so only a
    message frame reads this: it names the sender and gives a reply that works from the
    target. It is never written back into the environment, where the target project's
    ops and children would take the caller's IDs as their own.
    """

    model_config = ConfigDict(frozen=True)

    spawn_id: str
    project_root: Path
    cross_project: bool


class GlobalOptions(BaseModel):
    """Top-level options that apply to all commands."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    output: OutputConfig
    config_file: str | None = None
    harness: str | None = None
    yes: bool = False
    no_input: bool = False
    render_mode: RenderMode = "human"
    passthrough_args: tuple[str, ...] = ()
    sink: OutputSink | None = None
    explicit_format: OutputFormat | None = None
    project_root: Path | None = None
    directory_explicit: bool = False
    caller: Caller | None = None


class Emitter(Protocol):
    """Callable shape shared by command handlers that emit CLI results."""

    def __call__(
        self,
        payload: object,
        *,
        format_ctx: FormatContext | None = None,
    ) -> None: ...


_GLOBAL_OPTIONS: ContextVar[GlobalOptions] = ContextVar("_GLOBAL_OPTIONS")


def get_global_options() -> GlobalOptions:
    """Return parsed global options for the current command."""

    try:
        return _GLOBAL_OPTIONS.get()
    except LookupError:
        raise RuntimeError("global CLI options accessed outside an invocation scope") from None


@contextmanager
def invocation_scope(options: GlobalOptions) -> Generator[None]:
    """Install *options* for one CLI invocation and restore the prior context."""

    with warning_cache_scope():
        token = _GLOBAL_OPTIONS.set(options)
        try:
            yield
        finally:
            _GLOBAL_OPTIONS.reset(token)


def _resolve_sink(options: GlobalOptions) -> tuple[OutputSink, bool]:
    if options.sink is not None:
        return options.sink, False
    return create_sink(options.output), True


def current_output_sink() -> OutputSink:
    """Return the sink selected for the current invocation."""

    sink, _ = _resolve_sink(get_global_options())
    return sink


def is_agent_mode() -> bool:
    """Return whether the current invocation renders in agent mode."""

    return is_agent_render_mode(get_global_options().render_mode)


def emit(payload: object, *, format_ctx: FormatContext | None = None) -> None:
    """Write command output using current output format settings."""

    options = get_global_options()
    sink, flush_after = _resolve_sink(options)
    if isinstance(payload, CLIOutputProtocol):
        emit_output(
            payload.to_cli_output(
                format=options.output.format,
                explicit_format=options.explicit_format,
                agent_mode=is_agent_mode(),
            ),
            sink=sink,
            format_ctx=format_ctx,
        )
    else:
        emit_output(payload, sink=sink, format_ctx=format_ctx)
    if flush_after:
        flush_sink(sink)


def emit_error(message: str, *, exit_code: int = 1, name: str | None = None) -> Never:
    """Write a command error through the current sink and exit."""

    sink, flush_after = _resolve_sink(get_global_options())
    sink.error(message, exit_code=exit_code, name=name)
    if flush_after:
        flush_sink(sink)
    raise SystemExit(exit_code)


__all__ = [
    "Emitter",
    "GlobalOptions",
    "current_output_sink",
    "emit",
    "emit_error",
    "get_global_options",
    "invocation_scope",
    "is_agent_mode",
]
