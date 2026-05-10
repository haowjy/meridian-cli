"""Session log target resolution — detection preference, non-mutation, read-only contracts.

Tests that resolve_session_log_target reads state without reconciliation side-effects,
that detected transcripts take precedence without persisting the detected ID, and
that missing-transcript detection failures are not persisted.

# qa-validated: test-suite-redesign
"""

import json
import os
import time
from pathlib import Path

import pytest

from meridian.lib.ops.session_log import SessionLogInput, session_log_sync
from meridian.lib.ops.session_target import resolve_session_log_target
from meridian.lib.state import session_store, spawn_store
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


def _write_opencode_session(
    storage_root: Path,
    session_id: str,
    *events: dict[str, object],
) -> Path:
    session_path = storage_root / "session_diff" / f"{session_id}.json"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    return session_path


def test_session_log_chat_prefers_detected_transcript_without_mutating_tracked_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    home_root = tmp_path / "home"
    monkeypatch.setenv("HOME", home_root.as_posix())
    tracked_session_id = "ses_tracked_chat_stale"
    detected_session_id = "ses_detected_chat_real"
    log_path = _write_opencode_log(
        home_root / ".local" / "share" / "opencode" / "log",
        project_root, detected_session_id, "2026-03-08T12:00:05",
    )
    _write_opencode_session(
        home_root / ".local" / "share" / "opencode" / "storage",
        detected_session_id,
        {"role": "assistant", "content": "detected chat transcript"},
    )
    now = time.time()
    os.utime(log_path, (now, now))

    chat_id = session_store.start_session(
        runtime_root, harness="opencode", harness_session_id=tracked_session_id,
        model="gpt-5.3-codex", chat_id="c42",
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
            harness_session_id=tracked_session_id,
            started_at="2026-03-08T12:00:00Z",
        )

        output = session_log_sync(
            SessionLogInput(ref=chat_id, project_root=project_root.as_posix(), last_n=5)
        )

        assert output.session_id == detected_session_id
        assert output.source == "opencode transcript"
        assert [(message.role, message.content) for message in output.messages] == [
            ("assistant", "detected chat transcript")
        ]
        assert session_store.get_session_harness_id(runtime_root, chat_id) == tracked_session_id
        assert session_store.get_session_harness_ids(runtime_root, chat_id) == (tracked_session_id,)
        primary_spawn = spawn_store.get_spawn(runtime_root, "p42")
        assert primary_spawn is not None
        assert primary_spawn.harness_session_id == tracked_session_id
    finally:
        session_store.stop_session(runtime_root, chat_id)


def test_session_log_spawn_prefers_detected_transcript_without_mutating_tracked_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    home_root = tmp_path / "home"
    monkeypatch.setenv("HOME", home_root.as_posix())
    detected_session_id = "ses_detected_spawn_real"
    log_path = _write_opencode_log(
        home_root / ".local" / "share" / "opencode" / "log",
        project_root, detected_session_id, "2026-03-08T12:00:05",
    )
    _write_opencode_session(
        home_root / ".local" / "share" / "opencode" / "storage",
        detected_session_id,
        {"role": "assistant", "content": "detected spawn transcript"},
    )
    now = time.time()
    os.utime(log_path, (now, now))

    chat_id = session_store.start_session(
        runtime_root, harness="opencode", harness_session_id="",
        model="gpt-5.3-codex", chat_id="c42",
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

        output = session_log_sync(
            SessionLogInput(ref="p42", project_root=project_root.as_posix(), last_n=5)
        )

        assert output.session_id == detected_session_id
        assert output.source == "opencode transcript"
        assert [(message.role, message.content) for message in output.messages] == [
            ("assistant", "detected spawn transcript")
        ]
        assert session_store.get_session_harness_id(runtime_root, chat_id) == ""
        assert session_store.get_session_harness_ids(runtime_root, chat_id) == ("",)
        primary_spawn = spawn_store.get_spawn(runtime_root, "p42")
        assert primary_spawn is not None
        assert primary_spawn.harness_session_id == ""
    finally:
        session_store.stop_session(runtime_root, chat_id)


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
        project_root, detected_session_id, "2026-03-08T12:00:05",
    )
    now = time.time()
    os.utime(log_path, (now, now))

    chat_id = session_store.start_session(
        runtime_root, harness="opencode", harness_session_id="",
        model="gpt-5.3-codex", chat_id="c42",
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
                ref=chat_id, file_path=None,
                project_root=project_root, runtime_root=runtime_root,
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
        project_root, detected_session_id, "2026-03-08T12:00:05",
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
            ref="p42", file_path=None,
            project_root=project_root, runtime_root=runtime_root,
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
            ref="c999", file_path=None,
            project_root=project_root, runtime_root=runtime_root,
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
        runtime_root, chat_id="c1", model="gpt-5.4", agent="coder",
        harness="codex", prompt="hello", spawn_id="p1", harness_session_id=session_id,
    )
    state_path = runtime_root / "spawns" / "p1" / "state.json"
    before_state = state_path.read_text(encoding="utf-8")

    def _unexpected(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("reconciliation should not run for read-only target resolution")

    monkeypatch.setattr("meridian.lib.state.reaper.reconcile_spawns", _unexpected)
    monkeypatch.setattr("meridian.lib.state.reaper.reconcile_active_spawn", _unexpected)
    monkeypatch.setattr("meridian.lib.ops.spawn.query.read_spawn_row", _unexpected)

    resolved = resolve_session_log_target(
        ref="p1", file_path=None,
        project_root=project_root, runtime_root=runtime_root,
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
        runtime_root, harness="codex", harness_session_id=session_id,
        model="gpt-5.4", chat_id="c1",
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
        raise AssertionError("reconciliation should not run for read-only target resolution")

    monkeypatch.setattr("meridian.lib.state.reaper.reconcile_spawns", _unexpected)
    monkeypatch.setattr("meridian.lib.state.reaper.reconcile_active_spawn", _unexpected)
    monkeypatch.setattr("meridian.lib.ops.spawn.query.read_spawn_row", _unexpected)

    resolved = resolve_session_log_target(
        ref="c1", file_path=None,
        project_root=project_root, runtime_root=runtime_root,
    )

    assert resolved.session_id == session_id
    assert resolved.source == "codex transcript"
    assert state_path.read_text(encoding="utf-8") == before_state
