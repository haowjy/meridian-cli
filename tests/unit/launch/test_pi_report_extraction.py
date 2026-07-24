"""Pure Pi rejection extraction, error compaction, and retry classification."""

from __future__ import annotations

import json

import pytest

from meridian.lib.harness.extractors.pi import PI_EXTRACTOR as _PI_EXTRACTOR
from meridian.lib.harness.pi_failure import compact_pi_failure_output
from meridian.lib.launch.errors import should_retry
from meridian.lib.launch.report import extract_pi_failure_from_history

_ = _PI_EXTRACTOR  # Initialize harness modules before importing launch.report in isolation.


def test_extract_pi_failure_from_prompt_rejection() -> None:
    history = "\n".join(
        (
            '{"event_type":"meridian.pi.lifecycle.phase","payload":{"phase":"initial_prompt_sent"}}',
            '{"event_type":"response","payload":{"command":"prompt","success":false,'
            '"error":"No API key configured"}}',
            '{"event_type":"meridian.pi.lifecycle.phase","payload":{"phase":"cleanup_completed"}}',
        )
    )

    assert extract_pi_failure_from_history(history) == "No API key configured"


def test_extract_pi_failure_ignores_inject_rejection() -> None:
    history = json.dumps(
        {
            "event_type": "response",
            "harness_id": "pi",
            "payload": {
                "type": "response",
                "command": "prompt",
                "success": False,
                "error": "Future Pi prompt rejection",
                "meridian_control_action": "inject",
            },
        }
    )

    assert extract_pi_failure_from_history(history) is None


def test_compact_pi_failure_output_strips_extension_js_stack() -> None:
    message = (
        'Extension "/home/user/meridian-spawn-watch/index.js" error: '
        "extractMeridianSpawnIds is not defined\n"
        "  at Object.handleObservedMeridianSpawnOutput "
        "(file:///home/user/meridian-spawn-watch/index.js:1081:22)\n"
        "  at handleToolResult (file:///home/user/pi/tool.js:42:10)"
    )

    assert compact_pi_failure_output(message, verbose=False) == (
        'Extension "/home/user/meridian-spawn-watch/index.js" error: '
        "extractMeridianSpawnIds is not defined"
    )
    assert compact_pi_failure_output(message, verbose=True) == message.strip()


def test_compact_pi_failure_output_preserves_plain_multi_line_errors() -> None:
    message = "Mars model resolution failed\nAdd mars.toml or use a model id."

    assert compact_pi_failure_output(message, verbose=False) == message


@pytest.mark.parametrize(
    ("failure_message", "expected"),
    [
        ("No API key configured", False),
        ("Temporary provider failure", True),
    ],
)
def test_should_retry_classifies_pi_failures(
    failure_message: str,
    expected: bool,
) -> None:
    assert (
        should_retry(
            exit_code=1,
            stderr="",
            failure_message=failure_message,
            retries_attempted=0,
        )
        is expected
    )
