"""Session repair is explicit and separate from session-log read paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.ops.session_repair import (
    SessionRepairInput,
    repair_session_reference_sync,
)
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


def test_session_repair_updates_chat_and_primary_spawn_when_detectable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", runtime_root.as_posix())

    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", codex_home.as_posix())
    detected_session_id = "9473bc84-b0fc-4607-af64-47171be7ef73"
    _write_codex_rollout(
        sessions_root=codex_home / "sessions",
        project_root=project_root,
        session_id=detected_session_id,
        assistant_text="repair target transcript",
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
        before_spawn = spawn_store.get_spawn(runtime_root, "p42")
        assert before_spawn is not None
        before_spawn_id = before_spawn.harness_session_id or ""

        result = repair_session_reference_sync(
            SessionRepairInput(ref=chat_id, project_root=project_root.as_posix())
        )

        assert result.session_id == detected_session_id
        assert result.spawn_record_updated is (before_spawn_id != detected_session_id)
        assert session_store.get_session_harness_id(runtime_root, chat_id) == detected_session_id
        updated_spawn = spawn_store.get_spawn(runtime_root, "p42")
        assert updated_spawn is not None
        assert updated_spawn.harness_session_id == detected_session_id
    finally:
        session_store.stop_session(runtime_root, chat_id)


def test_session_repair_rejects_file_mode(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    session_file = tmp_path / "session.jsonl"
    session_file.write_text("[]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="requires REF; --file is read-only"):
        repair_session_reference_sync(
            SessionRepairInput(
                ref="",
                file_path=session_file.as_posix(),
                project_root=project_root.as_posix(),
            )
        )
