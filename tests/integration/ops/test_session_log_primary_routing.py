"""Session log source routing for managed primary spawns.

Tests that session_log_sync selects between live output, native transcript,
and artifact output correctly based on managed-primary state and spawn status.

# qa-validated: test-suite-redesign
"""

import json
import os
from pathlib import Path

from meridian.lib.launch.constants import HISTORY_FILENAME, PRIMARY_META_FILENAME
from meridian.lib.ops.session_log import SessionLogInput, session_log_sync
from meridian.lib.state import spawn_store
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
            "payload": {"item": {"type": "agentMessage", "text": "managed live progress"}},
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
            "payload": {"item": {"type": "agentMessage", "text": "managed fallback transcript"}},
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
            "payload": {"item": {"type": "agentMessage", "text": "managed artifact transcript"}},
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
            "payload": {"item": {"type": "agentMessage", "text": "managed opencode fallback"}},
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
