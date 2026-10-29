"""CLI sink formatting contracts."""

from __future__ import annotations

import io
import json

import pytest

from meridian.cli.output import JsonSink, TextSink


def test_json_sink_error_preserves_unnamed_and_named_shapes() -> None:
    stderr = io.StringIO()
    sink = JsonSink(stderr=stderr)

    sink.error("plain failure", exit_code=2)
    sink.error("named failure", exit_code=3, name="stable_name")

    lines = [json.loads(line) for line in stderr.getvalue().splitlines()]
    assert lines == [
        {"error": "plain failure", "exit_code": 2},
        {"error": "stable_name", "message": "named failure", "exit_code": 3},
    ]


def test_warning_and_error_shapes_follow_the_sink() -> None:
    text_stderr = io.StringIO()
    json_stderr = io.StringIO()

    TextSink(stderr=text_stderr).warning("heads up")
    TextSink(stderr=text_stderr).error("failed", exit_code=2, name="stable_name")
    JsonSink(stderr=json_stderr).warning("heads up")

    assert text_stderr.getvalue() == "warning: heads up\nerror: failed\n"
    assert json.loads(json_stderr.getvalue()) == {"warning": "heads up"}


@pytest.mark.parametrize(("method", "key"), [("status", "status"), ("heartbeat", "heartbeat")])
def test_json_sink_records_are_single_line_json(method: str, key: str) -> None:
    stderr = io.StringIO()

    getattr(JsonSink(stderr=stderr), method)("first line\nsecond line")

    assert stderr.getvalue() == f'{{"{key}":"first line\\nsecond line"}}\n'
    assert json.loads(stderr.getvalue()) == {key: "first line\nsecond line"}
