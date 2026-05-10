"""Session log retrieval via harness-specific file discovery.

Tests that session_log_sync resolves the correct native transcript file for
each harness type (opencode, codex, claude) using env-var overrides.

# qa-validated: test-suite-redesign
"""

import json
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


def test_session_log_resolves_opencode_storage_session_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    xdg_data_home = tmp_path / "xdg-data"
    session_id = "ses_fixture_session_12345"
    session_file = (
        xdg_data_home / "opencode" / "storage" / "session_diff" / f"{session_id}.json"
    )
    session_file.parent.mkdir(parents=True)
    session_file.write_text("[]\n", encoding="utf-8")
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
    assert output.segment_messages == 0
    assert output.messages == ()


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
