"""Session log source routing: live stream vs indexed history vs native files."""

import json
import os
from pathlib import Path

from meridian.lib.launch.constants import HISTORY_FILENAME, PRIMARY_META_FILENAME
from meridian.lib.ops.session_log import SessionLogInput, session_log_sync
from meridian.lib.ops.session_target import resolve_session_log_target
from meridian.lib.state import session_store, spawn_store
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


def _write_primary_meta(
    runtime_root: Path,
    spawn_id: str,
    *,
    managed_backend: bool = True,
) -> None:
    meta_path = runtime_root / "spawns" / spawn_id / PRIMARY_META_FILENAME
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {"managed_backend": managed_backend}
    if managed_backend:
        data["launcher_pid"] = os.getpid()
    meta_path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def _write_codex_rollout(
    *,
    home_root: Path,
    project_root: Path,
    session_id: str,
    assistant_text: str,
) -> Path:
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


def test_session_log_active_managed_primary_prefers_live_output_over_native_transcript(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
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
            "payload": {"item": {"type": "agentMessage", "text": "managed live progress"}},
        },
    )

    output = session_log_sync(
        SessionLogInput(ref="p42", project_root=project_root.as_posix(), tail=5)
    )

    assert output.session_id == "p42"
    assert output.source == "spawn p42 output"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "managed live progress")
    ]


def test_session_log_active_chat_reads_exact_native_transcript(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    home_root = tmp_path / "home"
    monkeypatch.setenv("HOME", home_root.as_posix())
    session_id = "c23dbbda-9729-493c-98d0-95df9e9aab92"
    _write_codex_rollout(
        home_root=home_root,
        project_root=project_root,
        session_id=session_id,
        assistant_text="native chat transcript should wait",
    )

    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id=session_id,
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
                "payload": {"item": {"type": "agentMessage", "text": "same live output"}},
            },
        )

        chat_target = resolve_session_log_target(
            ref=chat_id,
            file_path=None,
            project_root=project_root,
            runtime_root=runtime_root,
        )
        spawn_target = resolve_session_log_target(
            ref="p42",
            file_path=None,
            project_root=project_root,
            runtime_root=runtime_root,
        )
        chat_output = session_log_sync(
            SessionLogInput(ref=chat_id, project_root=project_root.as_posix(), tail=5)
        )

        assert chat_target.file_path != spawn_target.file_path
        assert chat_target.source == "codex transcript"
        assert spawn_target.source == "spawn p42 output"
        assert [(message.role, message.content) for message in chat_output.messages] == [
            ("assistant", "native chat transcript should wait")
        ]
    finally:
        session_store.stop_session(runtime_root, chat_id)


def test_session_log_completed_spawn_uses_indexed_history(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
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
    )
    spawn_store.finalize_spawn(runtime_root, "p42", "failed", 1, origin="runner")
    _write_primary_meta(runtime_root, "p42")
    _write_spawn_output(
        runtime_root,
        "p42",
        {
            "event_type": "item/completed",
            "harness_id": "codex",
            "payload": {"item": {"type": "agentMessage", "text": "managed artifact transcript"}},
        },
        artifact=True,
    )

    output = session_log_sync(
        SessionLogInput(ref="p42", project_root=project_root.as_posix(), tail=5)
    )

    assert output.session_id == "p42"
    assert output.source == "spawn p42 output"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "managed artifact transcript")
    ]
