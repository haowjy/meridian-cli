"""CLI sink formatting contracts."""

from __future__ import annotations

import io
import json

from meridian.cli.output import JsonSink, TextSink


def test_text_sink_error_ignores_machine_name() -> None:
    stderr = io.StringIO()

    TextSink(stderr=stderr).error("failed", exit_code=2, name="stable_name")

    assert stderr.getvalue() == "error: failed\n"


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


def test_warning_shapes_match_output_mode() -> None:
    text_stderr = io.StringIO()
    json_stderr = io.StringIO()

    TextSink(stderr=text_stderr).warning("heads up")
    JsonSink(stderr=json_stderr).warning("heads up")

    assert text_stderr.getvalue() == "warning: heads up\n"
    assert json.loads(json_stderr.getvalue()) == {"warning": "heads up"}


def test_json_sink_status_and_heartbeat_are_single_line_json() -> None:
    stderr = io.StringIO()
    sink = JsonSink(stderr=stderr)

    sink.status("first line\nsecond line")
    sink.heartbeat("still\nworking")

    lines = stderr.getvalue().splitlines()
    assert lines[0] + "\n" == '{"status":"first line\\nsecond line"}\n'
    assert json.loads(lines[0]) == {"status": "first line\nsecond line"}
    assert lines[1] + "\n" == '{"heartbeat":"still\\nworking"}\n'
    assert json.loads(lines[1]) == {"heartbeat": "still\nworking"}
