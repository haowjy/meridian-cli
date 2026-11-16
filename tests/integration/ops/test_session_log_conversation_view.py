"""`session log` shows the conversation by default; `--tools` adds tool traffic.

Entry ordinals are the same in every view, so a position printed by one view opens in another.
"""

import json
from pathlib import Path
from typing import Any

from meridian.lib.ops.session_log import SessionLogInput, SessionLogOutput, session_log_sync


def _text(role: str, text: str) -> dict[str, Any]:
    return {"type": role, "message": {"role": role, "content": text}}


def _tool_step(call_id: str, command: str, result: str) -> list[dict[str, Any]]:
    use = {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}
    done = {"type": "tool_result", "tool_use_id": call_id, "content": result}
    return [
        {"type": "assistant", "message": {"role": "assistant", "content": [use]}},
        {"type": "user", "message": {"role": "user", "content": [done]}},
    ]


def _claude_file(tmp_path: Path) -> Path:
    events: list[dict[str, Any]] = [
        {"type": "summary", "sessionId": "s1"},
        _text("user", "please check the logs"),
        _text("assistant", "looking"),
        *_tool_step("t1", "grep ERROR app.log", "app.log: ERROR boom"),
        # A message typed mid-turn is stored as an attachment, not as a user event.
        {
            "type": "attachment",
            "attachment": {
                "type": "queued_command",
                "commandMode": "prompt",
                "prompt": "also check the config",
            },
        },
        *_tool_step("t2", "cat config.toml", "key = 1"),
        _text("assistant", "found the error"),
    ]
    path = tmp_path / "claude.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return path


def _log(tmp_path: Path, **overrides: Any) -> SessionLogOutput:
    return session_log_sync(
        SessionLogInput(
            file_path=_claude_file(tmp_path).as_posix(),
            project_root=tmp_path.as_posix(),
            **overrides,
        )
    )


def _view(output: SessionLogOutput) -> list[tuple[int, str, str]]:
    return [(entry.index, entry.role, entry.content) for entry in output.entries]


def test_default_view_is_the_conversation_with_stable_ordinals(tmp_path: Path) -> None:
    output = _log(tmp_path, full=True)

    assert _view(output)[1:] == [
        (1, "user", "please check the logs"),
        (2, "assistant", "looking"),
        (3, "user", "also check the config"),
        (5, "assistant", "found the error"),
    ]
    assert "Tool calls, output and injected messages" in " ".join(output.hints)


def test_tools_view_shows_tool_traffic_under_the_same_ordinals(tmp_path: Path) -> None:
    output = _log(tmp_path, full=True, tools=True)

    assert [(index, role) for index, role, _ in _view(output)][1:] == [
        (1, "user"),
        (2, "assistant"),
        (3, "user"),
        (4, "assistant"),
        (5, "assistant"),
    ]
    assert "app.log: ERROR boom" in output.entries[2].content


def test_tail_counts_conversation_entries_and_navigation_keeps_the_view(tmp_path: Path) -> None:
    output = _log(tmp_path, tail=2)

    assert [index for index, _, _ in _view(output)] == [3, 5]
    assert output.previous_command is not None
    assert "--tools" not in output.previous_command

    with_tools = _log(tmp_path, tail=2, tools=True)
    assert with_tools.previous_command is not None
    assert "--tools" in with_tools.previous_command


def test_around_a_tool_only_entry_opens_in_the_default_view(tmp_path: Path) -> None:
    output = _log(tmp_path, around_ordinal=4, context=1)

    assert [index for index, _, _ in _view(output)] == [3, 5]


def test_codex_tool_calls_are_tool_traffic_and_injected_context_is_not_the_user(
    tmp_path: Path,
) -> None:
    def item(payload: dict[str, Any]) -> dict[str, Any]:
        return {"type": "response_item", "payload": payload}

    def message(role: str, text: str) -> dict[str, Any]:
        kind = "output_text" if role == "assistant" else "input_text"
        return item({"type": "message", "role": role, "content": [{"type": kind, "text": text}]})

    events = [
        {"type": "session_meta", "payload": {"id": "s1", "cwd": tmp_path.as_posix()}},
        message("user", "# AGENTS.md instructions for /repo\n\nBe terse."),
        message("user", "list the files"),
        item({"type": "custom_tool_call", "name": "exec", "input": "ls", "call_id": "c1"}),
        item(
            {
                "type": "custom_tool_call_output",
                "call_id": "c1",
                "output": [{"type": "input_text", "text": "a.py"}],
            }
        ),
        message("assistant", "one file: a.py"),
    ]
    path = tmp_path / "rollout.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

    def run(*, tools: bool) -> SessionLogOutput:
        return session_log_sync(
            SessionLogInput(
                file_path=path.as_posix(), project_root=tmp_path.as_posix(), full=True, tools=tools
            )
        )

    assert [(e.index, e.role) for e in run(tools=False).entries][1:] == [
        (2, "user"),
        (4, "assistant"),
    ]
    with_tools = run(tools=True).entries
    assert (with_tools[1].role, "Be terse." in with_tools[1].content) == ("injected", True)
    assert "a.py" in with_tools[3].content


def test_claude_user_events_split_into_the_user_and_everything_else(tmp_path: Path) -> None:
    def user(text: str, **fields: Any) -> dict[str, Any]:
        return {**_text("user", text), **fields}

    events = [
        {"type": "summary", "sessionId": "s1"},
        user("typed by the user", origin={"kind": "human"}),
        user("a plugin spoke", origin={"kind": "plugin"}),
        user("a meta message", isMeta=True),
        user("[Meridian] Message from spawn p1; it has no reply route here.\n\nhi"),
        user("Before this spawn closes, confirm whether you are actually finished."),
        user("a brief with no origin"),
        {
            "type": "attachment",
            "attachment": {
                "type": "queued_command",
                "commandMode": "task-notification",
                "prompt": "<task-notification>done</task-notification>",
            },
        },
        {"type": "system", "subtype": "compact_boundary"},
        user("This session is being continued from a previous conversation", isCompactSummary=True),
        user("after the compaction", origin={"kind": "human"}),
    ]
    path = tmp_path / "claude.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

    def run(*, tools: bool, segment: int) -> list[tuple[str, str]]:
        output = session_log_sync(
            SessionLogInput(
                file_path=path.as_posix(),
                project_root=tmp_path.as_posix(),
                full=True,
                tools=tools,
                segment=str(segment),
            )
        )
        return [(e.role, e.content.split("\n")[0][:30]) for e in output.entries]

    assert run(tools=False, segment=0)[1:] == [
        ("user", "typed by the user"),
        ("user", "a brief with no origin"),
    ]
    # Consecutive injected messages share an entry; the first holds the plugin, meta, Meridian
    # message and nudge, the last the queued task notification.
    assert [role for role, _ in run(tools=True, segment=0)] == [
        "system",
        "user",
        "injected",
        "user",
        "injected",
    ]
    # The compaction summary is the segment's entry 0, not a user entry.
    assert run(tools=True, segment=1) == [
        ("system", "This session is being continue"),
        ("user", "after the compaction"),
    ]
