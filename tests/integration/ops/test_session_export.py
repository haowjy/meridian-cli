"""Session export markdown regressions."""

import json
from pathlib import Path

from meridian.lib.ops.session_export import SessionExportInput, session_export_sync
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root


def test_session_export_stitches_all_compaction_segments_from_file(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    session_file.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"content": "hello"}}),
                json.dumps({"type": "system", "subtype": "compact_boundary"}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "text", "text": "hi **there**"},
                                {
                                    "type": "tool_use",
                                    "name": "bash",
                                    "input": {"command": "echo ok"},
                                },
                            ]
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {"type": "tool_result", "content": "ok\nmore output"},
                            ]
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    output = session_export_sync(SessionExportInput(file_path=session_file.as_posix()))

    assert output.markdown.startswith("# Session session\n")
    assert "**User:**\n\nhello" in output.markdown
    assert "**Assistant:**\n\nhi **there**" in output.markdown
    assert "---" not in output.markdown
    assert "### User" not in output.markdown
    assert "### Assistant" not in output.markdown
    assert "### Tool" not in output.markdown
    assert "<details>" not in output.markdown
    assert "> `echo ok`" in output.markdown
    assert "> ```text\n> ok\n> more output\n> ```" in output.markdown
    assert "compact_boundary" not in output.markdown


def test_session_export_separates_user_assistant_exchanges(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    session_file.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"content": "first"}}),
                json.dumps({"type": "assistant", "message": {"content": "reply"}}),
                json.dumps({"type": "user", "message": {"content": "second"}}),
                json.dumps({"type": "assistant", "message": {"content": "reply again"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    output = session_export_sync(SessionExportInput(file_path=session_file.as_posix()))

    assert (
        "**User:**\n\nfirst\n\n**Assistant:**\n\nreply\n\n---\n\n**User:**\n\nsecond"
        in output.markdown
    )


def test_session_export_truncates_long_tool_results(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    session_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "bash",
                                    "input": {"command": "meridian work list"},
                                },
                            ]
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "content": "\n".join(f"line {index}" for index in range(1, 10)),
                                },
                            ]
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    output = session_export_sync(SessionExportInput(file_path=session_file.as_posix()))

    assert "> `meridian work list` —" in output.markdown
    assert "> line 1" in output.markdown
    assert "> line 5" in output.markdown
    assert "> ... 4 more lines" in output.markdown
    assert "line 6" not in output.markdown


def test_session_export_include_spawns_appends_child_reports(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    session_file = tmp_path / "session.jsonl"
    session_file.write_text(
        json.dumps({"type": "assistant", "message": {"content": "done"}}) + "\n",
        encoding="utf-8",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.3-codex",
        agent="coder",
        harness="codex",
        prompt="child task",
        desc="Child task",
        spawn_id="p1",
        status="succeeded",
        started_at="2026-05-02T00:00:00Z",
    )
    report_path = runtime_root / "spawns" / "p1" / "report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("report body\n", encoding="utf-8")

    output = session_export_sync(
        SessionExportInput(
            ref="c1",
            file_path=session_file.as_posix(),
            include_spawns=True,
            project_root=project_root.as_posix(),
        )
    )

    assert "## Spawn Reports" in output.markdown
    assert "## Spawn: p1 — Child task" in output.markdown
    assert "report body" in output.markdown
