"""Unit tests for CLI output formatting behavior."""

from __future__ import annotations

from io import StringIO

from meridian.cli.output import TextSink, emit
from meridian.lib.core.util import FormatContext


class _Formattable:
    def __init__(self) -> None:
        self.seen: list[FormatContext | None] = []

    def format_text(self, ctx: FormatContext | None = None) -> str:
        self.seen.append(ctx)
        return "formatted"


def test_emit_uses_explicit_format_context_for_text_sink() -> None:
    stdout = StringIO()
    sink = TextSink(stdout=stdout)
    value = _Formattable()

    emit(value, sink=sink, format_ctx=FormatContext(verbosity=1, width=120))

    assert value.seen == [FormatContext(verbosity=1, width=120)]
    assert stdout.getvalue().strip() == "formatted"


def test_emit_uses_default_context_when_not_provided() -> None:
    stdout = StringIO()
    sink = TextSink(stdout=stdout)
    value = _Formattable()

    emit(value, sink=sink)

    assert value.seen == [FormatContext()]
    assert stdout.getvalue().strip() == "formatted"
