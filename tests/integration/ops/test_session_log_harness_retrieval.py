"""Session log retrieval via harness-specific file discovery.

Tests that session_log_sync resolves the correct native transcript file for
each harness type (opencode, codex, claude) using env-var overrides.

# qa-validated: test-suite-redesign
"""

import json
import sqlite3
from pathlib import Path

from meridian.lib.harness.claude import project_slug
from meridian.lib.ops.session_log import SessionLogInput, session_log_sync
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root


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


def _write_opencode_db_session(
    *,
    db_path: Path,
    session_id: str,
    messages: list[tuple[str, str]],
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            """
        )
        now = 1_778_945_817_030
        connection.execute(
            "INSERT INTO session (id, time_created, time_updated) VALUES (?, ?, ?)",
            (session_id, now, now),
        )
        for index, (role, text) in enumerate(messages):
            timestamp = now + index
            message_id = f"msg_{index}"
            part_id = f"prt_{index}"
            connection.execute(
                "INSERT INTO message "
                "(id, session_id, time_created, time_updated, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    message_id,
                    session_id,
                    timestamp,
                    timestamp,
                    json.dumps({"role": role, "time": {"created": timestamp}}),
                ),
            )
            connection.execute(
                "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    part_id,
                    message_id,
                    session_id,
                    timestamp,
                    timestamp,
                    json.dumps({"type": "text", "text": text}),
                ),
            )
        connection.commit()


def test_session_log_resolves_opencode_db_transcript_when_session_diff_is_empty(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    xdg_data_home = tmp_path / "xdg-data"
    session_id = "ses_fixture_session_12345"
    session_file = xdg_data_home / "opencode" / "storage" / "session_diff" / f"{session_id}.json"
    session_file.parent.mkdir(parents=True)
    session_file.write_text("[]\n", encoding="utf-8")
    _write_opencode_db_session(
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
            compaction=0,
            last_n=5,
            offset=0,
        )
    )

    assert output.session_id == session_id
    assert output.source == "opencode transcript"
    assert [(message.role, message.content) for message in output.messages] == [
        ("user", "show transcript please"),
        ("assistant", "here is your transcript"),
    ]


def test_session_log_resolves_codex_session_file_from_codex_home_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
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
            compaction=0,
            last_n=5,
            offset=0,
        )
    )

    assert output.session_id == session_id
    assert output.source == "codex transcript"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "codex env override transcript")
    ]


def test_session_log_resolves_claude_session_file_from_claude_config_dir_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
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
            compaction=0,
            last_n=5,
            offset=0,
        )
    )

    assert output.session_id == session_id
    assert output.source == "claude transcript"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "claude env override transcript")
    ]
