"""Per-invocation CLI options and output routing."""

from __future__ import annotations

from contextvars import ContextVar, Token
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
from meridian.lib.core.sink import OutputSink
from meridian.lib.core.util import FormatContext


class GlobalOptions(BaseModel):
    """Top-level options that apply to all commands."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    output: OutputConfig
    config_file: str | None = None
    harness: str | None = None
    yes: bool = False
    no_input: bool = False
    # Future cleanup: `output_explicit` may be removable now that
    # `explicit_format` carries the resolved explicit output selection.
    output_explicit: bool = False
    render_mode: RenderMode = "human"
    passthrough_args: tuple[str, ...] = ()
    sink: OutputSink | None = None
    explicit_format: OutputFormat | None = None
    project_root: Path | None = None
    directory_explicit: bool = False


class Emitter(Protocol):
    """Callable shape shared by command handlers that emit CLI results."""

    def __call__(
        self,
        payload: object,
        *,
        format_ctx: FormatContext | None = None,
    ) -> None: ...


_GLOBAL_OPTIONS: ContextVar[GlobalOptions | None] = ContextVar(
    "_GLOBAL_OPTIONS",
    default=None,
)


def has_global_options() -> bool:
    """Return whether the current context has parsed invocation options."""

    return _GLOBAL_OPTIONS.get() is not None


def get_global_options() -> GlobalOptions:
    """Return parsed global options for the current command."""

    default = GlobalOptions(output=OutputConfig(format="text"))
    return _GLOBAL_OPTIONS.get() or default


def set_global_options(options: GlobalOptions) -> Token[GlobalOptions | None]:
    """Install parsed options for the current invocation context."""

    return _GLOBAL_OPTIONS.set(options)


def reset_global_options(token: Token[GlobalOptions | None]) -> None:
    """Restore the invocation options represented by *token*."""

    _GLOBAL_OPTIONS.reset(token)


def _resolve_sink(options: GlobalOptions | None) -> tuple[OutputSink, bool]:
    if options is not None and options.sink is not None:
        return options.sink, False
    if options is None:
        return create_sink(OutputConfig(format="text")), True
    return create_sink(options.output), True


def current_output_sink() -> OutputSink:
    """Return the sink selected for the current invocation."""

    sink, _ = _resolve_sink(_GLOBAL_OPTIONS.get())
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

    sink, flush_after = _resolve_sink(_GLOBAL_OPTIONS.get())
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
    "has_global_options",
    "is_agent_mode",
    "reset_global_options",
    "set_global_options",
]
