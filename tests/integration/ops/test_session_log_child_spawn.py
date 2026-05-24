"""Session log output routing for child (non-primary) spawns and chat sessions.

Tests that session_log_sync picks the right output file (live vs artifact,
HISTORY_FILENAME vs OUTPUT_FILENAME) for child spawns and that missing
session IDs route correctly.

# qa-validated: test-suite-redesign
"""

import json
from pathlib import Path

import pytest

from meridian.lib.launch.constants import HISTORY_FILENAME, OUTPUT_FILENAME
from meridian.lib.ops.session_export import SessionExportInput, session_export_sync
from meridian.lib.ops.session_log import SessionLogInput, session_log_sync
from meridian.lib.ops.session_target import spawn_output_path_for_target
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
        from meridian.lib.ops.session_target import resolve_session_log_target

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
            "payload": {"item": {"type": "agentMessage", "text": "live progress"}},
        },
    )

    output = session_log_sync(
        SessionLogInput(ref="p42", project_root=project_root.as_posix(), tail=5)
    )

    assert output.session_id == "p42"
    assert output.source == "spawn p42 output"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "live progress")
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
            "payload": {"item": {"type": "agentMessage", "text": "live"}},
        },
        filename=OUTPUT_FILENAME,
    )
    _write_spawn_output(
        runtime_root,
        "p42",
        {
            "event_type": "item/completed",
            "payload": {"item": {"type": "agentMessage", "text": "artifact"}},
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


def test_session_log_active_child_spawn_prefers_live_output(tmp_path: Path) -> None:
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

    output = session_log_sync(
        SessionLogInput(ref="p42", project_root=project_root.as_posix(), tail=5)
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
            "payload": {"item": {"type": "agentMessage", "text": "artifact child transcript"}},
        },
        artifact=True,
    )

    output = session_log_sync(
        SessionLogInput(ref="p42", project_root=project_root.as_posix(), tail=5)
    )

    assert output.session_id == "p42"
    assert output.source == "spawn p42 output"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "artifact child transcript")
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
                "payload": {"item": {"type": "agentMessage", "text": "primary live progress"}},
            },
        )

        with pytest.raises(ValueError) as exc:
            session_log_sync(
                SessionLogInput(ref=chat_id, project_root=project_root.as_posix(), tail=5)
            )
        assert str(exc.value) == (
            "Session 'c42' exists but no transcript is available yet "
            "(no harness session id recorded)."
        )
    finally:
        session_store.stop_session(runtime_root, chat_id)


def test_session_export_spawn_duration_uses_last_attempt_exited_at(
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

    output = session_export_sync(
        SessionExportInput(ref="p42", project_root=project_root.as_posix())
    )

    assert "- Duration: 1m 5s" in output.markdown
