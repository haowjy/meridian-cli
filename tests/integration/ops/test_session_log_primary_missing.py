"""Session log retrieval for primary spawns/chats with missing harness session IDs.

Tests that cover detection fallback (scanning native harness logs), primary_meta
reading, and error behavior when no session ID is recorded for primary spawns.

# qa-validated: test-suite-redesign
"""

import json
import os
from pathlib import Path

import pytest

from meridian.lib.launch.constants import HISTORY_FILENAME, PRIMARY_META_FILENAME
from meridian.lib.ops.session_log import SessionLogInput, session_log_sync
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write


def _write_spawn_output(
    runtime_root: Path,
    spawn_id: str,
    *events: dict[str, object],
    filename: str = HISTORY_FILENAME,
) -> None:
    output_path = runtime_root / "spawns" / spawn_id / filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(json.dumps(event) for event in events) + "\n")


def _write_primary_meta(
    runtime_root: Path,
    spawn_id: str,
    *,
    managed_backend: bool = True,
    harness_session_id: str | None = None,
    harness_session_discovery: str | None = None,
    session_dir: str | None = None,
) -> None:
    meta_path = runtime_root / "spawns" / spawn_id / PRIMARY_META_FILENAME
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {"managed_backend": managed_backend}
    if managed_backend:
        data["launcher_pid"] = os.getpid()
    if harness_session_id is not None:
        data["harness_session_id"] = harness_session_id
    if harness_session_discovery is not None:
        data["harness_session_discovery"] = harness_session_discovery
    if session_dir is not None:
        data["session_dir"] = session_dir
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


def test_session_log_chat_missing_harness_session_id_detects_and_persists_primary_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
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
            SessionLogInput(ref=chat_id, project_root=project_root.as_posix(), tail=5)
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
    runtime_root = resolve_project_runtime_root_for_write(project_root)
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
        lambda **_kwargs: pytest.fail("detection should not run when primary_meta is set"),
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
            SessionLogInput(ref=chat_id, project_root=project_root.as_posix(), tail=5)
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
    runtime_root = resolve_project_runtime_root_for_write(project_root)
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
        SessionLogInput(ref="p42", project_root=project_root.as_posix(), tail=5)
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
    runtime_root = resolve_project_runtime_root_for_write(project_root)
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
        lambda **_kwargs: pytest.fail("detection should not run when primary_meta is set"),
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
        SessionLogInput(ref="p42", project_root=project_root.as_posix(), tail=5)
    )

    assert output.session_id == session_id
    assert output.source == "codex transcript"
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "meta-backed spawn transcript")
    ]
    primary_spawn = spawn_store.get_spawn(runtime_root, "p42")
    assert primary_spawn is not None
    assert primary_spawn.harness_session_id == ""


def test_session_log_primary_spawn_missing_harness_session_id_does_not_read_spawn_output(
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
        session_log_sync(SessionLogInput(ref="p42", project_root=project_root.as_posix(), tail=5))
    assert str(exc.value) == (
        "Spawn 'p42' has no transcript available yet (no harness session id recorded)."
    )


def test_session_log_primary_spawn_pi_never_created_skips_default_root_detection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id="c42",
        model="openai-codex/gpt-5.4-mini",
        agent="dev-orchestrator",
        harness="pi",
        kind="primary",
        prompt="do thing",
        harness_session_id="",
    )
    _write_primary_meta(
        runtime_root,
        "p42",
        managed_backend=False,
        harness_session_discovery="never_created",
        session_dir=(tmp_path / "custom-pi-sessions").as_posix(),
    )
    monkeypatch.setattr(
        "meridian.lib.ops.session_target._detect_primary_harness_session_id",
        lambda **_kwargs: pytest.fail("detection should be skipped for authoritative Pi metadata"),
    )

    with pytest.raises(ValueError) as exc:
        session_log_sync(SessionLogInput(ref="p42", project_root=project_root.as_posix(), tail=5))
    assert str(exc.value) == (
        "Spawn 'p42' has no transcript available yet (no harness session id recorded)."
    )
