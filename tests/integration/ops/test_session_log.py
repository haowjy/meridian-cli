"""Session log parser regressions."""

import json
import os
import time
from pathlib import Path

import pytest

from meridian.lib.harness.claude import project_slug
from meridian.lib.harness.transcript import DefaultTranscriptEventParser, parse_transcript_file
from meridian.lib.launch.constants import HISTORY_FILENAME, OUTPUT_FILENAME, PRIMARY_META_FILENAME
from meridian.lib.ops.session_log import SessionLogInput, session_log_sync
from meridian.lib.ops.session_target import resolve_session_log_target, spawn_output_path_for_target
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root


def _write_spawn_output(
    runtime_root: Path,
    spawn_id: str,
    *events: dict[str, object],
    artifact: bool = False,
    filename: str = HISTORY_FILENAME,
) -> None:
    base_dir = "artifacts" if artifact else "spawns"
    output_path = runtime_root / base_dir / spawn_id / filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(json.dumps(event) for event in events) + "\n")


def _write_primary_meta(
    runtime_root: Path,
    spawn_id: str,
    *,
    managed_backend: bool = True,
    launcher_pid: int | None = None,
    harness_session_id: str | None = None,
) -> None:
    meta_path = runtime_root / "spawns" / spawn_id / PRIMARY_META_FILENAME
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {"managed_backend": managed_backend}
    if launcher_pid is not None:
        data["launcher_pid"] = launcher_pid
    elif managed_backend:
        data["launcher_pid"] = os.getpid()
    if harness_session_id is not None:
        data["harness_session_id"] = harness_session_id
    meta_path.write_text(
        json.dumps(data) + "\n",
        encoding="utf-8",
    )


def _write_codex_rollout(
    *,
    home_root: Path | None = None,
    sessions_root: Path | None = None,
    project_root: Path,
    session_id: str,
    assistant_text: str,
) -> Path:
    if sessions_root is None:
        assert home_root is not None
        sessions_root = home_root / ".codex" / "sessions"

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


def _write_opencode_log(logs_dir: Path, project_root: Path, session_id: str, ts: str) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{session_id}.log"
    log_path.write_text(
        (
            f"INF {ts} +12ms service=session "
            f"id={session_id} directory={project_root.as_posix()} created\n"
        ),
        encoding="utf-8",
    )
    return log_path


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


def test_parse_session_file_splits_segments_on_compaction_boundary(tmp_path) -> None:
    session_file = tmp_path / "session.jsonl"
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "before boundary"}]},
            }
        ),
        json.dumps({"type": "system", "subtype": "compact_boundary"}),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "after boundary"}]},
            }
        ),
    ]
    session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    segments, total_compactions = parse_transcript_file(session_file)

    assert total_compactions == 1
    assert len(segments) == 2
    assert [(message.role, message.content) for message in segments[0]] == [
        ("assistant", "before boundary")
    ]
    assert [(message.role, message.content) for message in segments[1]] == [
        ("assistant", "after boundary")
    ]


def test_extract_from_event_claude_assistant_and_user_messages() -> None:
    assistant_messages, assistant_boundary = DefaultTranscriptEventParser().parse(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "assistant text"}]},
        }
    )
    user_messages, user_boundary = DefaultTranscriptEventParser().parse(
        {
            "type": "user",
            "message": {"content": [{"type": "text", "text": "user text"}]},
        }
    )

    assert assistant_boundary is False
    assert user_boundary is False
    assert [(message.role, message.content) for message in assistant_messages] == [
        ("assistant", "assistant text")
    ]
    assert [(message.role, message.content) for message in user_messages] == [("user", "user text")]


def test_extract_from_event_codex_response_and_exec_events() -> None:
    response_messages, response_boundary = DefaultTranscriptEventParser().parse(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "codex response"}],
            },
        }
    )
    exec_messages, exec_boundary = DefaultTranscriptEventParser().parse(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "codex exec"},
        }
    )

    assert response_boundary is False
    assert exec_boundary is False
    assert [(message.role, message.content) for message in response_messages] == [
        ("assistant", "codex response")
    ]
    assert [(message.role, message.content) for message in exec_messages] == [
        ("assistant", "codex exec")
    ]


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


def test_resolve_target_chat_missing_harness_session_id_reports_unavailable_transcript(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="",
        model="gpt-5.4",
        chat_id="c1",
    )

    try:
        with pytest.raises(ValueError) as exc:
            resolve_session_log_target(
                ref=chat_id,
                file_path=None,
                project_root=project_root,
                runtime_root=runtime_root,
            )
        assert str(exc.value) == (
            "Session 'c1' exists but no transcript is available yet "
            "(no harness session id recorded)."
        )
    finally:
        session_store.stop_session(runtime_root, chat_id)


def test_session_log_spawn_missing_harness_session_id_reads_live_output(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id="c42",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="do thing",
        harness_session_id="",
    )
    _write_spawn_output(
        runtime_root,
        "p42",
        {
            "event_type": "item/completed",
            "harness_id": "codex",
            "payload": {
                "item": {"type": "agentMessage", "text": "live progress"},
            },
        },
    )

    output = session_log_sync(
        SessionLogInput(ref="p42", project_root=project_root.as_posix(), last_n=5)
    )

    assert output.session_id == "p42"
    assert output.source == "spawn p42 output"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "live progress")
    ]


def test_session_log_spawn_missing_harness_session_id_reads_legacy_live_output(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id="c42",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="do thing",
        harness_session_id="",
    )
    _write_spawn_output(
        runtime_root,
        "p42",
        {
            "event_type": "item/completed",
            "harness_id": "codex",
            "payload": {
                "item": {"type": "agentMessage", "text": "legacy live progress"},
            },
        },
        filename=OUTPUT_FILENAME,
    )

    output = session_log_sync(
        SessionLogInput(ref="p42", project_root=project_root.as_posix(), last_n=5)
    )

    assert output.session_id == "p42"
    assert output.source == "spawn p42 output"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "legacy live progress")
    ]


def test_spawn_output_path_legacy_precedence_with_both_files(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    _write_spawn_output(
        runtime_root,
        "p42",
        {
            "event_type": "item/completed",
            "payload": {"item": {"type": "agentMessage", "text": "legacy live"}},
        },
        filename=OUTPUT_FILENAME,
    )
    _write_spawn_output(
        runtime_root,
        "p42",
        {
            "event_type": "item/completed",
            "payload": {"item": {"type": "agentMessage", "text": "legacy artifact"}},
        },
        artifact=True,
        filename=OUTPUT_FILENAME,
    )

    assert spawn_output_path_for_target(runtime_root, "p42", live_first=True) == (
        runtime_root / "spawns" / "p42" / OUTPUT_FILENAME
    )
    assert spawn_output_path_for_target(runtime_root, "p42", live_first=False) == (
        runtime_root / "artifacts" / "p42" / OUTPUT_FILENAME
    )


def test_session_log_active_child_spawn_prefers_live_output(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id="c42",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="do thing",
        harness_session_id="missing-native-session",
        status="running",
    )
    _write_spawn_output(
        runtime_root,
        "p42",
        {
            "event_type": "item/completed",
            "harness_id": "codex",
            "payload": {
                "item": {"type": "agentMessage", "text": "live child progress"},
            },
        },
    )
    _write_spawn_output(
        runtime_root,
        "p42",
        {
            "event_type": "item/completed",
            "harness_id": "codex",
            "payload": {
                "item": {"type": "agentMessage", "text": "artifact child progress"},
            },
        },
        artifact=True,
    )

    output = session_log_sync(
        SessionLogInput(ref="p42", project_root=project_root.as_posix(), last_n=5)
    )

    assert output.session_id == "p42"
    assert output.source == "spawn p42 output"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "live child progress")
    ]


def test_session_log_child_spawn_falls_back_to_artifact_output_when_native_unavailable(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id="c42",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="do thing",
        harness_session_id="missing-native-session",
        status="failed",
    )
    _write_spawn_output(
        runtime_root,
        "p42",
        {
            "event_type": "item/completed",
            "harness_id": "codex",
            "payload": {
                "item": {"type": "agentMessage", "text": "artifact child transcript"},
            },
        },
        artifact=True,
    )

    output = session_log_sync(
        SessionLogInput(ref="p42", project_root=project_root.as_posix(), last_n=5)
    )

    assert output.session_id == "p42"
    assert output.source == "spawn p42 output"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "artifact child transcript")
    ]


def test_session_log_child_spawn_falls_back_to_legacy_artifact_output_when_native_unavailable(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id="c42",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="do thing",
        harness_session_id="missing-native-session",
        status="failed",
    )
    _write_spawn_output(
        runtime_root,
        "p42",
        {
            "event_type": "item/completed",
            "harness_id": "codex",
            "payload": {
                "item": {"type": "agentMessage", "text": "legacy artifact transcript"},
            },
        },
        artifact=True,
        filename=OUTPUT_FILENAME,
    )

    output = session_log_sync(
        SessionLogInput(ref="p42", project_root=project_root.as_posix(), last_n=5)
    )

    assert output.session_id == "p42"
    assert output.source == "spawn p42 output"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "legacy artifact transcript")
    ]


def test_session_log_chat_missing_harness_session_id_does_not_read_primary_spawn_output(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="",
        model="gpt-5.4",
        chat_id="c42",
    )
    try:
        spawn_store.start_spawn(
            runtime_root,
            spawn_id="p42",
            chat_id=chat_id,
            model="gpt-5.4",
            agent="dev-orchestrator",
            harness="codex",
            kind="primary",
            prompt="do thing",
            harness_session_id="",
        )
        _write_spawn_output(
            runtime_root,
            "p42",
            {
                "event_type": "item/completed",
                "harness_id": "codex",
                "payload": {
                    "item": {"type": "agentMessage", "text": "primary live progress"},
                },
            },
        )

        with pytest.raises(ValueError) as exc:
            session_log_sync(
                SessionLogInput(ref=chat_id, project_root=project_root.as_posix(), last_n=5)
            )
        assert str(exc.value) == (
            "Session 'c42' exists but no transcript is available yet "
            "(no harness session id recorded)."
        )
    finally:
        session_store.stop_session(runtime_root, chat_id)


def test_session_log_chat_missing_harness_session_id_detects_and_persists_primary_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    home_root = tmp_path / "home"
    monkeypatch.setenv("HOME", home_root.as_posix())
    session_id = "c13d8c7b-1506-4ef5-9137-c6a677f45c15"
    _write_codex_rollout(
        home_root=home_root,
        project_root=project_root,
        session_id=session_id,
        assistant_text="native codex transcript",
    )

    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="",
        model="gpt-5.4",
        chat_id="c42",
    )
    try:
        spawn_store.start_spawn(
            runtime_root,
            spawn_id="p42",
            chat_id=chat_id,
            model="gpt-5.4",
            agent="dev-orchestrator",
            harness="codex",
            kind="primary",
            prompt="do thing",
            harness_session_id="",
            started_at="2026-01-01T00:00:00Z",
        )

        output = session_log_sync(
            SessionLogInput(ref=chat_id, project_root=project_root.as_posix(), last_n=5)
        )

        assert output.session_id == session_id
        assert output.source == "codex transcript"
        assert [(message.role, message.content) for message in output.messages] == [
            ("assistant", "native codex transcript")
        ]
        assert session_store.get_session_harness_id(runtime_root, chat_id) == ""
        primary_spawn = spawn_store.get_spawn(runtime_root, "p42")
        assert primary_spawn is not None
        assert primary_spawn.harness_session_id == ""
    finally:
        session_store.stop_session(runtime_root, chat_id)


def test_session_log_chat_missing_harness_session_id_reads_primary_meta_session_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    home_root = tmp_path / "home"
    monkeypatch.setenv("HOME", home_root.as_posix())
    session_id = "52ea07a8-5fbe-410f-b5f4-f0a9ec4a7315"
    _write_codex_rollout(
        home_root=home_root,
        project_root=project_root,
        session_id=session_id,
        assistant_text="meta-backed chat transcript",
    )
    monkeypatch.setattr(
        "meridian.lib.ops.session_target._detect_primary_harness_session_id",
        lambda **_kwargs: pytest.fail("session detection should not run when primary_meta is set"),
    )

    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="",
        model="gpt-5.4",
        chat_id="c42",
    )
    try:
        spawn_store.start_spawn(
            runtime_root,
            spawn_id="p42",
            chat_id=chat_id,
            model="gpt-5.4",
            agent="dev-orchestrator",
            harness="codex",
            kind="primary",
            prompt="do thing",
            harness_session_id="",
        )
        _write_primary_meta(runtime_root, "p42", harness_session_id=session_id)

        output = session_log_sync(
            SessionLogInput(ref=chat_id, project_root=project_root.as_posix(), last_n=5)
        )

        assert output.session_id == session_id
        assert output.source == "codex transcript"
        assert [(message.role, message.content) for message in output.messages] == [
            ("assistant", "meta-backed chat transcript")
        ]
        assert session_store.get_session_harness_id(runtime_root, chat_id) == ""
        primary_spawn = spawn_store.get_spawn(runtime_root, "p42")
        assert primary_spawn is not None
        assert primary_spawn.harness_session_id == ""
    finally:
        session_store.stop_session(runtime_root, chat_id)


def test_session_log_primary_spawn_missing_harness_session_id_detects_native_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    home_root = tmp_path / "home"
    monkeypatch.setenv("HOME", home_root.as_posix())
    session_id = "4e6a6145-bc68-4317-a00e-03904e03dfe8"
    _write_codex_rollout(
        home_root=home_root,
        project_root=project_root,
        session_id=session_id,
        assistant_text="native spawn transcript",
    )

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id="c42",
        model="gpt-5.4",
        agent="dev-orchestrator",
        harness="codex",
        kind="primary",
        prompt="do thing",
        harness_session_id="",
        started_at="2026-01-01T00:00:00Z",
    )

    output = session_log_sync(
        SessionLogInput(ref="p42", project_root=project_root.as_posix(), last_n=5)
    )

    assert output.session_id == session_id
    assert output.source == "codex transcript"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "native spawn transcript")
    ]
    primary_spawn = spawn_store.get_spawn(runtime_root, "p42")
    assert primary_spawn is not None
    assert primary_spawn.harness_session_id == ""


def test_session_log_primary_spawn_missing_harness_session_id_reads_primary_meta_session_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    home_root = tmp_path / "home"
    monkeypatch.setenv("HOME", home_root.as_posix())
    session_id = "bc5e81d8-f91f-4e37-a728-9e9a24a026cf"
    _write_codex_rollout(
        home_root=home_root,
        project_root=project_root,
        session_id=session_id,
        assistant_text="meta-backed spawn transcript",
    )
    monkeypatch.setattr(
        "meridian.lib.ops.session_target._detect_primary_harness_session_id",
        lambda **_kwargs: pytest.fail("session detection should not run when primary_meta is set"),
    )

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id="c42",
        model="gpt-5.4",
        agent="dev-orchestrator",
        harness="codex",
        kind="primary",
        prompt="do thing",
        harness_session_id="",
    )
    _write_primary_meta(runtime_root, "p42", harness_session_id=session_id)

    output = session_log_sync(
        SessionLogInput(ref="p42", project_root=project_root.as_posix(), last_n=5)
    )

    assert output.session_id == session_id
    assert output.source == "codex transcript"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "meta-backed spawn transcript")
    ]
    primary_spawn = spawn_store.get_spawn(runtime_root, "p42")
    assert primary_spawn is not None
    assert primary_spawn.harness_session_id == ""


def test_session_log_active_managed_primary_prefers_live_output_over_native_transcript(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    home_root = tmp_path / "home"
    monkeypatch.setenv("HOME", home_root.as_posix())
    session_id = "3e9a0285-2c37-4311-96f5-2ec5c0d7c6c7"
    _write_codex_rollout(
        home_root=home_root,
        project_root=project_root,
        session_id=session_id,
        assistant_text="native managed primary transcript",
    )

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id="c42",
        model="gpt-5.4",
        agent="dev-orchestrator",
        harness="codex",
        kind="primary",
        prompt="do thing",
        harness_session_id=session_id,
        status="running",
    )
    _write_primary_meta(runtime_root, "p42")
    _write_spawn_output(
        runtime_root,
        "p42",
        {
            "event_type": "item/completed",
            "harness_id": "codex",
            "payload": {
                "item": {"type": "agentMessage", "text": "managed live progress"},
            },
        },
    )

    output = session_log_sync(
        SessionLogInput(ref="p42", project_root=project_root.as_posix(), last_n=5)
    )

    assert output.session_id == "p42"
    assert output.source == "spawn p42 output"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "managed live progress")
    ]


def test_session_log_completed_managed_primary_prefers_native_transcript(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    home_root = tmp_path / "home"
    monkeypatch.setenv("HOME", home_root.as_posix())
    session_id = "9f7f0edf-1cdf-4701-a9ce-679f58aab0f9"
    _write_codex_rollout(
        home_root=home_root,
        project_root=project_root,
        session_id=session_id,
        assistant_text="native completed managed transcript",
    )

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id="c42",
        model="gpt-5.4",
        agent="dev-orchestrator",
        harness="codex",
        kind="primary",
        prompt="do thing",
        harness_session_id=session_id,
        status="failed",
    )
    _write_primary_meta(runtime_root, "p42")
    _write_spawn_output(
        runtime_root,
        "p42",
        {
            "event_type": "item/completed",
            "harness_id": "codex",
            "payload": {
                "item": {"type": "agentMessage", "text": "managed fallback transcript"},
            },
        },
        artifact=True,
    )

    output = session_log_sync(
        SessionLogInput(ref="p42", project_root=project_root.as_posix(), last_n=5)
    )

    assert output.session_id == session_id
    assert output.source == "codex transcript"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "native completed managed transcript")
    ]


def test_session_log_completed_managed_primary_falls_back_to_output_when_native_unavailable(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id="c42",
        model="gpt-5.4",
        agent="dev-orchestrator",
        harness="codex",
        kind="primary",
        prompt="do thing",
        harness_session_id="missing-native-session",
        status="failed",
    )
    _write_primary_meta(runtime_root, "p42")
    _write_spawn_output(
        runtime_root,
        "p42",
        {
            "event_type": "item/completed",
            "harness_id": "codex",
            "payload": {
                "item": {"type": "agentMessage", "text": "managed artifact transcript"},
            },
        },
        artifact=True,
    )

    output = session_log_sync(
        SessionLogInput(ref="p42", project_root=project_root.as_posix(), last_n=5)
    )

    assert output.session_id == "p42"
    assert output.source == "spawn p42 output"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "managed artifact transcript")
    ]


def test_managed_primary_fallback_to_legacy_output(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id="c42",
        model="gpt-5.4",
        agent="dev-orchestrator",
        harness="codex",
        kind="primary",
        prompt="do thing",
        harness_session_id="missing-native-session",
        status="failed",
    )
    _write_primary_meta(runtime_root, "p42")
    _write_spawn_output(
        runtime_root,
        "p42",
        {
            "event_type": "item/completed",
            "harness_id": "codex",
            "payload": {
                "item": {"type": "agentMessage", "text": "managed legacy transcript"},
            },
        },
        artifact=True,
        filename=OUTPUT_FILENAME,
    )

    output = session_log_sync(
        SessionLogInput(ref="p42", project_root=project_root.as_posix(), last_n=5)
    )

    assert output.session_id == "p42"
    assert output.source == "spawn p42 output"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "managed legacy transcript")
    ]


def test_session_log_completed_managed_opencode_fallback_is_best_effort(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id="c42",
        model="gpt-5.3-codex",
        agent="dev-orchestrator",
        harness="opencode",
        kind="primary",
        prompt="do thing",
        harness_session_id="missing-native-session",
        status="failed",
    )
    _write_primary_meta(runtime_root, "p42")
    _write_spawn_output(
        runtime_root,
        "p42",
        {
            "event_type": "item/completed",
            "harness_id": "opencode",
            "payload": {
                "item": {"type": "agentMessage", "text": "managed opencode fallback"},
            },
        },
        artifact=True,
    )

    output = session_log_sync(
        SessionLogInput(ref="p42", project_root=project_root.as_posix(), last_n=5)
    )

    assert output.session_id == "p42"
    assert output.source is not None
    assert "best-effort" in output.source
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "managed opencode fallback")
    ]


def test_session_log_primary_spawn_missing_harness_session_id_does_not_read_spawn_output(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id="c42",
        model="gpt-5.4",
        agent="dev-orchestrator",
        harness="codex",
        kind="primary",
        prompt="do thing",
        harness_session_id="",
    )
    _write_spawn_output(
        runtime_root,
        "p42",
        {
            "event_type": "item/completed",
            "harness_id": "codex",
            "payload": {
                "item": {"type": "agentMessage", "text": "primary live progress"},
            },
        },
    )

    with pytest.raises(ValueError) as exc:
        session_log_sync(
            SessionLogInput(ref="p42", project_root=project_root.as_posix(), last_n=5)
        )
    assert str(exc.value) == (
        "Spawn 'p42' has no transcript available yet "
        "(no harness session id recorded)."
    )


def test_resolve_target_chat_detected_primary_session_without_transcript_is_not_persisted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    home_root = tmp_path / "home"
    monkeypatch.setenv("HOME", home_root.as_posix())
    detected_session_id = "ses_missing_storage_chat"
    log_path = _write_opencode_log(
        home_root / ".local" / "share" / "opencode" / "log",
        project_root,
        detected_session_id,
        "2026-03-08T12:00:05",
    )
    now = time.time()
    os.utime(log_path, (now, now))

    chat_id = session_store.start_session(
        runtime_root,
        harness="opencode",
        harness_session_id="",
        model="gpt-5.3-codex",
        chat_id="c42",
    )
    try:
        spawn_store.start_spawn(
            runtime_root,
            spawn_id="p42",
            chat_id=chat_id,
            model="gpt-5.3-codex",
            agent="dev-orchestrator",
            harness="opencode",
            kind="primary",
            prompt="do thing",
            harness_session_id="",
            started_at="2026-03-08T12:00:00Z",
        )

        with pytest.raises(FileNotFoundError):
            resolve_session_log_target(
                ref=chat_id,
                file_path=None,
                project_root=project_root,
                runtime_root=runtime_root,
            )

        assert session_store.get_session_harness_id(runtime_root, chat_id) == ""
        primary_spawn = spawn_store.get_spawn(runtime_root, "p42")
        assert primary_spawn is not None
        assert primary_spawn.harness_session_id == ""
    finally:
        session_store.stop_session(runtime_root, chat_id)


def test_resolve_target_spawn_detected_primary_session_without_transcript_is_not_persisted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    home_root = tmp_path / "home"
    monkeypatch.setenv("HOME", home_root.as_posix())
    detected_session_id = "ses_missing_storage_spawn"
    log_path = _write_opencode_log(
        home_root / ".local" / "share" / "opencode" / "log",
        project_root,
        detected_session_id,
        "2026-03-08T12:00:05",
    )
    now = time.time()
    os.utime(log_path, (now, now))

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id="c42",
        model="gpt-5.3-codex",
        agent="dev-orchestrator",
        harness="opencode",
        kind="primary",
        prompt="do thing",
        harness_session_id="",
        started_at="2026-03-08T12:00:00Z",
    )

    with pytest.raises(FileNotFoundError):
        resolve_session_log_target(
            ref="p42",
            file_path=None,
            project_root=project_root,
            runtime_root=runtime_root,
        )

    primary_spawn = spawn_store.get_spawn(runtime_root, "p42")
    assert primary_spawn is not None
    assert primary_spawn.harness_session_id == ""


def test_resolve_target_chat_not_found_preserves_missing_chat_error(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError) as exc:
        resolve_session_log_target(
            ref="c999",
            file_path=None,
            project_root=project_root,
            runtime_root=runtime_root,
        )
    assert str(exc.value) == "Chat 'c999' not found"


def test_resolve_target_spawn_id_uses_read_only_lookup_without_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        assistant_text="spawn transcript",
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
    )
    state_path = runtime_root / "spawns" / "p1" / "state.json"
    before_state = state_path.read_text(encoding="utf-8")

    def _unexpected(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("reconciliation helper should not run for read-only target resolution")

    monkeypatch.setattr("meridian.lib.state.reaper.reconcile_spawns", _unexpected)
    monkeypatch.setattr("meridian.lib.state.reaper.reconcile_active_spawn", _unexpected)
    monkeypatch.setattr("meridian.lib.ops.spawn.query.read_spawn_row", _unexpected)

    resolved = resolve_session_log_target(
        ref="p1",
        file_path=None,
        project_root=project_root,
        runtime_root=runtime_root,
    )

    assert resolved.session_id == session_id
    assert resolved.source == "codex transcript"
    assert state_path.read_text(encoding="utf-8") == before_state


def test_resolve_target_chat_id_uses_read_only_lookup_without_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", codex_home.as_posix())
    session_id = "6f2c95c5-f617-4e4d-80ab-d98f3270bcaf"
    _write_codex_rollout(
        sessions_root=codex_home / "sessions",
        project_root=project_root,
        session_id=session_id,
        assistant_text="chat transcript",
    )

    session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id=session_id,
        model="gpt-5.4",
        chat_id="c1",
    )
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p1",
        chat_id="c1",
        model="gpt-5.4",
        agent="dev-orchestrator",
        harness="codex",
        kind="primary",
        prompt="do thing",
        harness_session_id=session_id,
        status="running",
    )
    state_path = runtime_root / "spawns" / "p1" / "state.json"
    before_state = state_path.read_text(encoding="utf-8")

    def _unexpected(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("reconciliation helper should not run for read-only target resolution")

    monkeypatch.setattr("meridian.lib.state.reaper.reconcile_spawns", _unexpected)
    monkeypatch.setattr("meridian.lib.state.reaper.reconcile_active_spawn", _unexpected)
    monkeypatch.setattr("meridian.lib.ops.spawn.query.read_spawn_row", _unexpected)

    resolved = resolve_session_log_target(
        ref="c1",
        file_path=None,
        project_root=project_root,
        runtime_root=runtime_root,
    )

    assert resolved.session_id == session_id
    assert resolved.source == "codex transcript"
    assert state_path.read_text(encoding="utf-8") == before_state
