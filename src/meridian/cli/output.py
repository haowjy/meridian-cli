"""CLI output formatting utilities."""

import json
import sys
from typing import Any, Literal, Protocol, TextIO, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.sink import NullSink, OutputSink
from meridian.lib.core.util import FormatContext, TextFormattable, to_jsonable

OutputFormat = Literal["text", "json"]
type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

_DEFAULT_FORMAT_CTX = FormatContext()


class OutputConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    format: OutputFormat
    suppress_events: bool = False


@runtime_checkable
class CLIOutputProtocol(Protocol):
    """Payload types with custom CLI output shaping implement this."""

    def to_cli_output(
        self,
        *,
        format: str,
        explicit_format: str | None,
        agent_mode: bool,
    ) -> object: ...


class _FlushableSink(Protocol):
    def flush(self) -> None: ...


def _to_json_value(value: Any) -> JSONValue:
    return cast("JSONValue", to_jsonable(value))


def normalize_output_format(
    *,
    requested: str | None,
    json_mode: bool,
) -> OutputFormat:
    """Resolve final output format from global output flags."""

    if json_mode:
        return "json"

    if requested is None or requested == "":
        return "text"

    normalized = requested.strip().lower()
    if normalized in {"text", "json"}:
        return cast("OutputFormat", normalized)
    raise SystemExit("--format must be one of: text, json")


def resolve_effective_format(
    *,
    explicit_format: OutputFormat | None,
    agent_mode: bool,
    agent_default_format: Literal["text", "json"] | None,
) -> OutputFormat:
    """Resolve the effective output format for a command.

    Resolution order:
    1. Explicit format from --format/--json always wins
    2. In agent mode, use operation's agent_default_format if set
    3. Fall back to "text"
    """

    if explicit_format is not None:
        return explicit_format
    if agent_mode and agent_default_format is not None:
        return agent_default_format
    return "text"


def _render_text(value: Any, format_ctx: FormatContext | None = None) -> str:
    # "text" mode: prefer format_text() if available, fall back to indented JSON
    # for types that have not yet implemented the protocol.
    if isinstance(value, TextFormattable):
        return value.format_text(format_ctx or _DEFAULT_FORMAT_CTX)
    return json.dumps(_to_json_value(value), sort_keys=True, indent=2)


class TextSink:
    def __init__(
        self,
        *,
        format: OutputFormat = "text",
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        emit_events: bool = True,
    ) -> None:
        self._format = format
        self._stdout = sys.stdout if stdout is None else stdout
        self._stderr = sys.stderr if stderr is None else stderr
        self._emit_events = emit_events

    def result(self, payload: Any) -> None:
        self._render_result(payload, format_ctx=None)

    def result_with_context(self, payload: Any, *, format_ctx: FormatContext) -> None:
        self._render_result(payload, format_ctx=format_ctx)

    def _render_result(self, payload: Any, *, format_ctx: FormatContext | None) -> None:
        if isinstance(payload, str):
            if payload == "":
                return
            print(payload, file=self._stdout)
            return
        rendered = _render_text(payload, format_ctx)
        if rendered == "":
            return
        print(rendered, file=self._stdout)

    def status(self, message: str) -> None:
        print(message, file=self._stderr)

    def warning(self, message: str) -> None:
        print(f"warning: {message}", file=self._stderr)

    def error(self, message: str, exit_code: int = 1) -> None:
        _ = exit_code
        print(f"error: {message}", file=self._stderr)

    def heartbeat(self, message: str) -> None:
        print(message, file=self._stderr, flush=True)

    def event(self, payload: dict[str, Any]) -> None:
        if not self._emit_events:
            return
        print(
            json.dumps(_to_json_value(payload), separators=(",", ":")),
            file=self._stderr,
            flush=True,
        )

    def flush(self) -> None:
        self._stdout.flush()
        self._stderr.flush()


class JsonSink:
    def __init__(
        self,
        *,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        emit_events: bool = True,
    ) -> None:
        self._stdout = sys.stdout if stdout is None else stdout
        self._stderr = sys.stderr if stderr is None else stderr
        self._result: JSONValue | str | None = None
        self._has_result = False
        self._emit_events = emit_events

    def result(self, payload: Any) -> None:
        self._result = _to_json_value(payload)
        self._has_result = True

    def status(self, message: str) -> None:
        print(message, file=self._stderr)

    def warning(self, message: str) -> None:
        print(f"warning: {message}", file=self._stderr)

    def error(self, message: str, exit_code: int = 1) -> None:
        payload = {"error": message, "exit_code": exit_code}
        print(
            json.dumps(_to_json_value(payload), separators=(",", ":")),
            file=self._stderr,
        )

    def heartbeat(self, message: str) -> None:
        print(message, file=self._stderr, flush=True)

    def event(self, payload: dict[str, Any]) -> None:
        if not self._emit_events:
            return
        print(
            json.dumps(_to_json_value(payload), separators=(",", ":")),
            file=self._stderr,
            flush=True,
        )

    def flush(self) -> None:
        if self._has_result:
            print(json.dumps(self._result, sort_keys=True), file=self._stdout)
        self._stdout.flush()
        self._stderr.flush()


_NULL_SINK = NullSink()


def create_sink(config: OutputConfig) -> OutputSink:
    if config.format == "json":
        return JsonSink(emit_events=not config.suppress_events)
    if config.format == "text":
        return TextSink(format=config.format, emit_events=not config.suppress_events)
    return _NULL_SINK


def flush_sink(sink: OutputSink) -> None:
    flush = getattr(sink, "flush", None)
    if callable(flush):
        try:
            cast("_FlushableSink", sink).flush()
        except Exception:
            # Flush should never fail command execution (e.g. closed stdio on shutdown).
            return


def emit(value: Any, *, sink: OutputSink, format_ctx: FormatContext | None = None) -> None:
    """Emit one payload via an explicit output sink."""

    if format_ctx is not None and isinstance(sink, TextSink):
        sink.result_with_context(value, format_ctx=format_ctx)
        return
    sink.result(value)
