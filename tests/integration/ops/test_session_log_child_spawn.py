"""Session log output routing for child (non-primary) spawns and primary sessions.

Tests that session_log_sync picks the right output file (live vs artifact)
for child spawns and that missing session IDs route correctly.

# qa-validated: test-suite-redesign
"""

import json
from pathlib import Path

import pytest

from meridian.lib.core.native_identity import NativeSessionUnavailable
from meridian.lib.launch.constants import HISTORY_FILENAME
from meridian.lib.ops.session_export import SessionExportInput, session_export_sync
from meridian.lib.ops.session_log import SessionLogInput, session_log_sync
from meridian.lib.ops.session_search import SessionSearchInput, session_search_sync
from meridian.lib.ops.spawn.query import detail_from_row
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.artifact_store import LocalStore, make_artifact_key
from meridian.lib.state.paths import resolve_project_runtime_root_for_write


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


def _write_codex_rollout(
    *,
    sessions_root: Path,
    project_root: Path,
    session_id: str,
    assistant_text: str,
) -> None:
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


def test_unbound_spawn_does_not_read_runner_history(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
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
            "payload": {"item": {"type": "agentMessage", "text": "live progress"}},
        },
    )

    with pytest.raises(NativeSessionUnavailable, match="unbound"):
        session_log_sync(SessionLogInput(ref="p42", project_root=project_root.as_posix()))


def test_session_log_child_spawn_without_harness_id_does_not_use_parent_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", codex_home.as_posix())
    _write_codex_rollout(
        sessions_root=codex_home / "sessions",
        project_root=project_root,
        session_id="parent-session-id",
        assistant_text="parent transcript should not appear",
    )
    parent_chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="parent-session-id",
        model="gpt-5.4",
        chat_id="c-parent",
        kind="primary",
    )
    try:
        spawn_store.start_spawn(
            runtime_root,
            spawn_id="p42",
            chat_id=parent_chat_id,
            parent_id="p-parent",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="do child thing",
            harness_session_id="",
        )
        spawn_store.finalize_spawn(runtime_root, "p42", "failed", 1, origin="runner")

        with pytest.raises(ValueError):
            session_log_sync(
                SessionLogInput(ref="p42", project_root=project_root.as_posix(), tail=5)
            )
    finally:
        session_store.stop_session(runtime_root, parent_chat_id)


def test_session_log_child_spawn_uses_authoritative_child_chat_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", codex_home.as_posix())
    _write_codex_rollout(
        sessions_root=codex_home / "sessions",
        project_root=project_root,
        session_id="11111111-1111-4111-8111-111111111111",
        assistant_text="child transcript",
    )
    child_chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="11111111-1111-4111-8111-111111111111",
        native_store=(codex_home / "sessions").as_posix(),
        model="gpt-5.4",
        chat_id="c-child",
        spawn_id="p42",
    )
    try:
        spawn_store.start_spawn(
            runtime_root,
            spawn_id="p42",
            chat_id=child_chat_id,
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="do child thing",
            harness_session_id="",
        )
        spawn_store.finalize_spawn(runtime_root, "p42", "failed", 1, origin="runner")

        output = session_log_sync(
            SessionLogInput(ref="p42", project_root=project_root.as_posix(), tail=5)
        )

        assert output.session_id == "11111111-1111-4111-8111-111111111111"
        assert output.source == "codex transcript"
        assert [(message.role, message.content) for message in output.messages] == [
            ("assistant", "child transcript")
        ]
    finally:
        session_store.stop_session(runtime_root, child_chat_id)


def test_unbound_active_child_ignores_live_runner_history(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
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
            "payload": {"item": {"type": "agentMessage", "text": "live child progress"}},
        },
    )
    _write_spawn_output(
        runtime_root,
        "p42",
        {
            "event_type": "item/completed",
            "harness_id": "codex",
            "payload": {"item": {"type": "agentMessage", "text": "artifact child progress"}},
        },
        artifact=True,
    )

    with pytest.raises(NativeSessionUnavailable, match="unbound"):
        session_log_sync(SessionLogInput(ref="p42", project_root=project_root.as_posix()))


def test_legacy_runner_artifacts_do_not_authorize_transcript_reads(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
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
    canonical_events = (
        {
            "event_type": "meridian.pi.lifecycle.phase",
            "payload": {"phase": "canonical_phase"},
        },
        {
            "event_type": "item/completed",
            "harness_id": "codex",
            "payload": {"item": {"type": "agentMessage", "text": "canonical marker"}},
        },
    )
    legacy_events = (
        {
            "event_type": "meridian.pi.lifecycle.phase",
            "payload": {"phase": "legacy_phase"},
        },
        {
            "event_type": "item/completed",
            "harness_id": "codex",
            "payload": {"item": {"type": "agentMessage", "text": "legacy marker"}},
        },
    )
    _write_spawn_output(runtime_root, "p42", *canonical_events)
    _write_spawn_output(runtime_root, "p42", *legacy_events, artifact=True)

    artifacts = LocalStore(root_dir=runtime_root / "artifacts")
    history_key = make_artifact_key("p42", HISTORY_FILENAME)
    row = spawn_store.get_spawn(runtime_root, "p42")
    assert row is not None
    detail = detail_from_row(
        project_root=project_root,
        row=row,
        include_report_body=False,
        runtime_root=runtime_root,
    )
    with pytest.raises(ValueError, match="not a native transcript"):
        session_log_sync(SessionLogInput(file_path=str(runtime_root / "spawns/p42/history.jsonl")))
    with pytest.raises(ValueError, match="not a native transcript"):
        session_search_sync(SessionSearchInput(
            file_path=str(runtime_root / "spawns/p42/history.jsonl"), query="canonical marker",
        ))

    assert not (runtime_root / "spawns" / "p42" / "native-transcript.jsonl").exists()
    assert b"canonical marker" not in artifacts.get(history_key)
    assert b"legacy marker" in artifacts.get(history_key)
    assert artifacts.list_artifacts("p42").count(history_key) == 1
    # Pi phases come from the pi-lifecycle.json sidecar, never from runner history.
    assert detail.pi_lifecycle_phase is None


def test_session_log_chat_reads_file_authority_without_harness_session_id(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
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
                "payload": {"item": {"type": "agentMessage", "text": "primary live progress"}},
            },
        )

        with pytest.raises(ValueError, match="no verified native session for c42"):
            session_log_sync(
                SessionLogInput(ref=chat_id, project_root=project_root.as_posix(), tail=5)
            )
    finally:
        session_store.stop_session(runtime_root, chat_id)


def test_session_export_spawn_duration_uses_last_attempt_exited_at(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id="c42",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="do thing",
        started_at="2026-04-12T14:00:00Z",
    )
    spawn_store.record_spawn_exited(
        runtime_root,
        "p42",
        exit_code=0,
        exited_at="2026-04-12T14:01:05Z",
    )
    _write_spawn_output(
        runtime_root,
        "p42",
        {
            "event_type": "item/completed",
            "harness_id": "codex",
            "payload": {"item": {"type": "agentMessage", "text": "done"}},
        },
    )

    session_id = "11111111-1111-4111-8111-111111111111"
    _write_codex_rollout(
        sessions_root=tmp_path / "native",
        project_root=project_root,
        session_id=session_id,
        assistant_text="done",
    )
    session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id=session_id,
        native_store=str(tmp_path / "native"),
        model="test",
        chat_id="c42",
    )
    output = session_export_sync(
        SessionExportInput(ref="p42", project_root=project_root.as_posix())
    )

    assert "- Duration: 1m 5s" in output.markdown


def test_session_export_include_spawns_groups_by_owner_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", codex_home.as_posix())

    primary_session_id = "22222222-2222-4222-8222-222222222222"
    owner_chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id=primary_session_id,
        native_store=(codex_home / "sessions").as_posix(),
        model="gpt-5.4",
        chat_id="c-owner",
        kind="primary",
    )
    primary_spawn_id = str(
        spawn_store.start_spawn(
            runtime_root,
            spawn_id="p-primary",
            chat_id=owner_chat_id,
            owner_chat_id=owner_chat_id,
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            kind="primary",
            prompt="primary",
            harness_session_id=primary_session_id,
        )
    )
    child_spawn_id = str(
        spawn_store.start_spawn(
            runtime_root,
            chat_id="c-child",
            owner_chat_id=owner_chat_id,
            parent_id=primary_spawn_id,
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            kind="child",
            prompt="child task",
            harness_session_id="thread-child",
        )
    )
    _write_codex_rollout(
        sessions_root=codex_home / "sessions",
        project_root=project_root,
        session_id=primary_session_id,
        assistant_text="primary transcript",
    )
    report_path = runtime_root / "spawns" / child_spawn_id / "report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("Child spawn report body.\n", encoding="utf-8")

    try:
        output = session_export_sync(
            SessionExportInput(
                ref=owner_chat_id,
                project_root=project_root.as_posix(),
                include_spawns=True,
            )
        )
    finally:
        session_store.stop_session(runtime_root, owner_chat_id)

    assert "## Spawn Reports" in output.markdown
    assert f"## Spawn: {child_spawn_id}" in output.markdown
    assert "Child spawn report body." in output.markdown
