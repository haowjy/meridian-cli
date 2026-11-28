import json
from pathlib import Path

from meridian.lib.harness.agy.transcript import AgyReader
from meridian.lib.harness.transcript import (
    INJECTED_ROLE,
    iter_transcript_events,
    parse_transcript_events_with_prologues,
)
from meridian.lib.state.native_identity import NativeKeyFields

READER = AgyReader()
_SYSTEM = (
    "The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n\n<SYSTEM_MESSAGE>\n"
    "injected by a PreInvocation hook:\nMessage from x [meridian:abc]\nhi\n</SYSTEM_MESSAGE>"
)
_USER = "<USER_REQUEST>\nls\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nm\n</ADDITIONAL_METADATA>"
_CALL = {"name": "run_command", "args": {"CommandLine": "ls"}}
_INJECTED = "injected by a PreInvocation hook:\nMessage from x [meridian:abc]\nhi"


def test_parses_steps_skips_unknown_and_survives_torn_last_line(tmp_path: Path) -> None:
    steps = [
        {"type": "SYSTEM_MESSAGE", "source": "SYSTEM_SDK", "content": _SYSTEM},
        {
            "type": "USER_INPUT",
            "source": "USER_EXPLICIT",
            "content": _USER,
        },
        {
            "type": "PLANNER_RESPONSE",
            "content": None,
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": "ls"}}],
        },
        {"type": "GENERIC", "content": "Created At: t\nCompleted At: t\na.txt"},
        {"type": "FUTURE_KIND", "content": "x"},
        {"type": "ERROR_MESSAGE", "content": "", "error": "RESOURCE_EXHAUSTED (code 429)"},
        {"type": "CHECKPOINT", "content": "summary"},
        {"type": "PLANNER_RESPONSE", "content": "done", "tool_calls": None},
    ]
    path = tmp_path / "transcript_full.jsonl"
    path.write_text(
        "".join(json.dumps(s) + "\n" for s in steps) + '{"type":"PLANNER_RESPONSE","con'
    )

    parsed = parse_transcript_events_with_prologues(
        iter_transcript_events(path), parser=READER.transcript_parser()
    )

    assert len(parsed.segments) == 2
    assert parsed.segment_setups[1] == "summary"
    got = [
        (m.role, m.content, m.tool_call.name if m.tool_call else None)
        for segment in parsed.segments
        for m in segment
    ]
    assert got == [
        (
            INJECTED_ROLE,
            "injected by a PreInvocation hook:\nMessage from x [meridian:abc]\nhi",
            None,
        ),
        ("user", "ls", None),
        ("assistant", "[tool: run_command ls]", "bash"),
        ("user", "[tool_result] a.txt", None),
        ("annotation", "RESOURCE_EXHAUSTED (code 429)", None),
        ("assistant", "done", None),
    ]
    assert parsed.rendering_reason is not None


def test_key_completes_from_conversation_id_alone() -> None:
    key = NativeKeyFields("agy", session_id="c0ffee").complete()
    assert key is not None
    assert key.native_store.endswith(".gemini/antigravity-cli/brain")
    assert NativeKeyFields("claude", session_id="c0ffee").complete() is None
