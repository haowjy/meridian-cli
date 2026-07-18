"""Session log retrieval via harness-specific file discovery.

Tests that session_log_sync resolves the correct native transcript file for
each harness type (opencode, codex, claude) using env-var overrides.

# qa-validated: test-suite-redesign
"""

import json
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from meridian.lib.harness.claude import project_slug
from meridian.lib.launch.constants import HISTORY_FILENAME
from meridian.lib.ops.session_log import SessionLogInput, session_log_sync
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from tests.support.opencode_db import (
    write_opencode_db_session,
    write_opencode_db_session_with_parts,
)


def _write_codex_rollout(
    *,
    sessions_root: Path,
    project_root: Path,
    session_id: str,
    assistant_text: str,
) -> Path:
    rollout_dir = sessions_root / "2026" / "04"
    rollout_dir.mkdir(parents=True, exist_ok=True)
    rollout_path = rollout_dir / f"rollout-2026-04-22T00-00-00-{session_id}.jsonl"
    rollout_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": session_id, "cwd": project_root.as_posix()},
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": assistant_text}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return rollout_path


def _write_claude_session(
    *,
    config_root: Path,
    project_root: Path,
    session_id: str,
    assistant_text: str,
) -> Path:
    project_dir = config_root / "projects" / project_slug(project_root)
    project_dir.mkdir(parents=True, exist_ok=True)
    session_path = project_dir / f"{session_id}.jsonl"
    session_path.write_text(
        "\n".join(
            [
                json.dumps({"sessionId": session_id}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": assistant_text}]},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return session_path


def test_session_log_resolves_opencode_db_transcript_when_session_diff_is_empty(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    xdg_data_home = tmp_path / "xdg-data"
    session_id = "ses_fixture_session_12345"
    session_file = xdg_data_home / "opencode" / "storage" / "session_diff" / f"{session_id}.json"
    session_file.parent.mkdir(parents=True)
    session_file.write_text("[]\n", encoding="utf-8")
    write_opencode_db_session(
        db_path=xdg_data_home / "opencode" / "opencode.db",
        session_id=session_id,
        messages=[
            ("user", "show transcript please"),
            ("assistant", "here is your transcript"),
        ],
    )
    monkeypatch.setenv("XDG_DATA_HOME", xdg_data_home.as_posix())

    spawn_store.start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.3-codex",
        agent="coder",
        harness="opencode",
        prompt="hello",
        spawn_id="p1",
        harness_session_id=session_id,
        started_at="2026-04-11T00:00:00Z",
    )

    output = session_log_sync(
        SessionLogInput(
            ref="p1",
            project_root=project_root.as_posix(),
            tail=5,
        )
    )

    assert output.session_id == session_id
    assert output.source == "opencode transcript"
    assert [(message.role, message.content) for message in output.messages] == [
        ("user", "show transcript please"),
        ("assistant", "here is your transcript"),
    ]


def test_session_log_resolves_opencode_db_without_legacy_session_file(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    xdg_data_home = tmp_path / "xdg-data"
    session_id = "ses_fixture_db_only_12345"
    write_opencode_db_session_with_parts(
        db_path=xdg_data_home / "opencode" / "opencode.db",
        session_id=session_id,
        messages=[
            (
                "user",
                {"system": "OpenCode DB setup"},
                [{"type": "text", "text": "show transcript please"}],
            ),
            ("assistant", {}, [{"type": "text", "text": "here is your transcript"}]),
        ],
    )
    monkeypatch.setenv("XDG_DATA_HOME", xdg_data_home.as_posix())

    chat_id = "c1"
    session_store.start_session(
        runtime_root,
        harness="opencode",
        harness_session_id=session_id,
        model="gpt-5.3-codex",
        chat_id=chat_id,
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id=chat_id,
        model="gpt-5.3-codex",
        agent="coder",
        harness="opencode",
        prompt="hello",
        spawn_id="p1",
        harness_session_id=session_id,
        started_at="2026-04-11T00:00:00Z",
    )

    for ref in ("p1", chat_id, session_id):
        output = session_log_sync(
            SessionLogInput(
                ref=ref,
                project_root=project_root.as_posix(),
                full=True,
            )
        )

        assert output.session_id == session_id
        assert output.source == "opencode transcript"
        assert output.entries[0].role == "system"
        assert output.entries[0].content == "OpenCode DB setup"
        assert [(message.role, message.content) for message in output.messages] == [
            ("user", "show transcript please"),
            ("assistant", "here is your transcript"),
        ]


def test_session_log_renders_opencode_db_completed_tool_parts(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    xdg_data_home = tmp_path / "xdg-data"
    session_id = "ses_fixture_db_tool_12345"
    target_path = "/tmp/opencode-write.txt"
    write_opencode_db_session_with_parts(
        db_path=xdg_data_home / "opencode" / "opencode.db",
        session_id=session_id,
        messages=[
            ("user", {}, [{"type": "text", "text": "write the file"}]),
            (
                "assistant",
                {},
                [
                    {"type": "reasoning", "text": "hidden"},
                    {
                        "type": "tool",
                        "tool": "write",
                        "state": {
                            "status": "completed",
                            "input": {"filePath": target_path, "content": "OK\n"},
                            "output": "Wrote file successfully.",
                        },
                    },
                ],
            ),
            ("assistant", {}, [{"type": "text", "text": "File written."}]),
        ],
    )
    monkeypatch.setenv("XDG_DATA_HOME", xdg_data_home.as_posix())

    spawn_store.start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.3-codex",
        agent="coder",
        harness="opencode",
        prompt="hello",
        spawn_id="p1",
        harness_session_id=session_id,
        started_at="2026-04-11T00:00:00Z",
    )

    output = session_log_sync(
        SessionLogInput(
            ref="p1",
            project_root=project_root.as_posix(),
            full=True,
            truncate=False,
        )
    )

    assert [(message.role, message.content) for message in output.messages] == [
        ("user", "write the file"),
        ("assistant", f"[tool: write {target_path}]"),
        ("user", "[tool_result] Wrote file successfully."),
        ("assistant", "File written."),
    ]
    assert output.messages[1].tool_call is not None
    assert output.messages[1].tool_call.name == "write"
    assert output.messages[1].tool_call.body == target_path
    assert output.messages[2].is_tool_result is True


def test_session_log_default_render_shows_completed_opencode_task_result(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    xdg_data_home = tmp_path / "xdg-data"
    session_id = "ses_fixture_db_task_12345"
    write_opencode_db_session_with_parts(
        db_path=xdg_data_home / "opencode" / "opencode.db",
        session_id=session_id,
        messages=[
            ("user", {}, [{"type": "text", "text": "find KB links"}]),
            (
                "assistant",
                {},
                [
                    {"type": "text", "text": "Let me now search for contradictions."},
                    {
                        "type": "tool",
                        "tool": "task",
                        "state": {
                            "status": "completed",
                            "input": {"description": "Search code repo for KB links"},
                            "output": (
                                '<task id="ses_child" state="completed">\n'
                                "<task_result>\n"
                                "Here is the complete report of all matches found.\n"
                                "</task_result>\n"
                                "</task>"
                            ),
                        },
                    },
                ],
            ),
        ],
    )
    monkeypatch.setenv("XDG_DATA_HOME", xdg_data_home.as_posix())

    spawn_store.start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.3-codex",
        agent="coder",
        harness="opencode",
        prompt="hello",
        spawn_id="p1",
        harness_session_id=session_id,
        started_at="2026-04-11T00:00:00Z",
    )

    output = session_log_sync(
        SessionLogInput(
            ref="p1",
            project_root=project_root.as_posix(),
            from_ordinal=1,
            limit=1,
        )
    )
    rendered = output.format_text()

    assert "task: Search code repo for KB links" in rendered
    assert "(completed) Here is the complete report of all matches found." in rendered


def test_session_log_renders_opencode_db_compaction_as_segment_handoff(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    xdg_data_home = tmp_path / "xdg-data"
    session_id = "ses_fixture_db_compaction_12345"
    write_opencode_db_session_with_parts(
        db_path=xdg_data_home / "opencode" / "opencode.db",
        session_id=session_id,
        messages=[
            ("user", {}, [{"type": "text", "text": "segment zero user"}]),
            ("assistant", {}, [{"type": "text", "text": "segment zero assistant"}]),
            (
                "assistant",
                {"mode": "compaction", "agent": "compaction"},
                [{"type": "text", "text": "handoff into segment one"}],
            ),
            ("user", {}, [{"type": "text", "text": "segment one user"}]),
            ("assistant", {}, [{"type": "text", "text": "segment one assistant"}]),
        ],
    )
    monkeypatch.setenv("XDG_DATA_HOME", xdg_data_home.as_posix())

    spawn_store.start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.3-codex",
        agent="coder",
        harness="opencode",
        prompt="hello",
        spawn_id="p1",
        harness_session_id=session_id,
        started_at="2026-04-11T00:00:00Z",
    )

    output = session_log_sync(
        SessionLogInput(
            ref="p1",
            project_root=project_root.as_posix(),
            full=True,
        )
    )

    assert output.total_segments == 2
    assert output.segment_index == 1
    assert output.entries[0].role == "system"
    assert output.entries[0].content == "handoff into segment one"
    assert [(message.role, message.content) for message in output.messages] == [
        ("user", "segment one user"),
        ("assistant", "segment one assistant"),
    ]


def test_session_log_falls_back_to_legacy_opencode_json_when_db_has_no_messages(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    xdg_data_home = tmp_path / "xdg-data"
    session_id = "ses_fixture_empty_db_legacy_json_12345"
    session_file = xdg_data_home / "opencode" / "storage" / "session_diff" / f"{session_id}.json"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(
        json.dumps({"role": "assistant", "content": "legacy JSON transcript"}) + "\n",
        encoding="utf-8",
    )
    write_opencode_db_session_with_parts(
        db_path=xdg_data_home / "opencode" / "opencode.db",
        session_id=session_id,
        messages=[],
    )
    monkeypatch.setenv("XDG_DATA_HOME", xdg_data_home.as_posix())

    spawn_store.start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.3-codex",
        agent="coder",
        harness="opencode",
        prompt="hello",
        spawn_id="p1",
        harness_session_id=session_id,
        started_at="2026-04-11T00:00:00Z",
    )

    output = session_log_sync(
        SessionLogInput(
            ref="p1",
            project_root=project_root.as_posix(),
            full=True,
        )
    )

    assert output.session_id == session_id
    assert output.source == "opencode transcript"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "legacy JSON transcript")
    ]


def _write_spawn_history(runtime_root: Path, spawn_id: str, text: str) -> None:
    history_path = runtime_root / "spawns" / spawn_id / HISTORY_FILENAME
    history_path.write_text(
        json.dumps(
            {
                "event_type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("ref_kind", "ref", "track_session", "expected_session_id", "history_text"),
    [
        ("spawn", "p1", False, "p1", "spawn history transcript"),
        ("chat", "c1", True, "c1", "chat ref spawn history transcript"),
        ("raw", "session", True, "session", "raw ref spawn history transcript"),
        (
            "raw-untracked",
            "session",
            False,
            "session",
            "untracked raw ref spawn history transcript",
        ),
    ],
)
def test_session_log_falls_back_to_spawn_history_when_opencode_db_has_no_messages(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    ref_kind: str,
    ref: str,
    track_session: bool,
    expected_session_id: str,
    history_text: str,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    xdg_data_home = tmp_path / "xdg-data"
    session_id = f"ses_fixture_empty_db_{ref_kind}_history_12345"
    write_opencode_db_session_with_parts(
        db_path=xdg_data_home / "opencode" / "opencode.db",
        session_id=session_id,
        messages=[],
    )
    monkeypatch.setenv("XDG_DATA_HOME", xdg_data_home.as_posix())

    if track_session:
        session_store.start_session(
            runtime_root,
            harness="opencode",
            harness_session_id=session_id,
            model="gpt-5.3-codex",
            chat_id="c1",
            spawn_id="p1",
        )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.3-codex",
        agent="coder",
        harness="opencode",
        prompt="hello",
        spawn_id="p1",
        harness_session_id=session_id,
        started_at="2026-04-11T00:00:00Z",
    )
    _write_spawn_history(runtime_root, "p1", history_text)

    resolved_ref = session_id if ref == "session" else ref
    output = session_log_sync(
        SessionLogInput(ref=resolved_ref, project_root=project_root.as_posix(), full=True)
    )

    expected_output_session_id = (
        session_id if expected_session_id == "session" else expected_session_id
    )
    assert output.session_id == expected_output_session_id
    assert output.source == "spawn p1 output"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", history_text)
    ]


def test_session_log_resolves_codex_session_file_from_codex_home_env(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", codex_home.as_posix())
    session_id = "78f02237-df5f-43fe-a6e5-929f98287877"
    _write_codex_rollout(
        sessions_root=codex_home / "sessions",
        project_root=project_root,
        session_id=session_id,
        assistant_text="codex env override transcript",
    )

    spawn_store.start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="hello",
        spawn_id="p1",
        harness_session_id=session_id,
        started_at="2026-04-11T00:00:00Z",
    )

    output = session_log_sync(
        SessionLogInput(
            ref="p1",
            project_root=project_root.as_posix(),
            tail=5,
        )
    )

    assert output.session_id == session_id
    assert output.source == "codex transcript"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "codex env override transcript")
    ]


def test_session_log_resolves_claude_session_file_from_claude_config_dir_env(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    claude_config_dir = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", claude_config_dir.as_posix())
    session_id = "claude-env-session"
    _write_claude_session(
        config_root=claude_config_dir,
        project_root=project_root,
        session_id=session_id,
        assistant_text="claude env override transcript",
    )

    spawn_store.start_spawn(
        runtime_root,
        chat_id="c1",
        model="claude-opus",
        agent="coder",
        harness="claude",
        prompt="hello",
        spawn_id="p1",
        harness_session_id=session_id,
        started_at="2026-04-11T00:00:00Z",
    )

    output = session_log_sync(
        SessionLogInput(
            ref="p1",
            project_root=project_root.as_posix(),
            tail=5,
        )
    )

    assert output.session_id == session_id
    assert output.source == "claude transcript"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "claude env override transcript")
    ]
