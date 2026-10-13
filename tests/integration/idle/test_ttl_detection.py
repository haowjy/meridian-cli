"""Recorded native identity integration for idle cache-TTL detection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.config.settings import MeridianConfig
from meridian.lib.harness.claude_idle import detect_ttl
from meridian.lib.idle.service import IdleService
from meridian.lib.state import session_store
from meridian.lib.state.idle_store import IdleStore
from meridian.lib.state.paths import resolve_project_runtime_root_for_write


@pytest.mark.integration
@pytest.mark.parametrize(("chat_id", "expected_ttl"), [("c1", 3600), ("missing", None)])
def test_claude_ttl_uses_the_recorded_store_after_process_cwd_drifts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chat_id: str,
    expected_ttl: int | None,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "launch-project"
    drifted_cwd = tmp_path / "different-working-directory"
    native_store = tmp_path / "claude-native-store"
    for directory in (home, project, drifted_cwd, native_store):
        directory.mkdir()
    monkeypatch.setenv("HOME", home.as_posix())
    monkeypatch.setenv("MERIDIAN_HOME", (home / ".meridian").as_posix())

    session_id = "05f66a37-e6c2-4425-aa4b-78bf9f6bb700"
    (native_store / f"{session_id}.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"sessionId": session_id}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"usage": {"cache_creation": {"ephemeral_1h_input_tokens": 42}}},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    runtime_root = resolve_project_runtime_root_for_write(project)
    session_store.start_session(
        runtime_root,
        harness="claude",
        harness_session_id=session_id,
        model="claude-opus-4-6",
        chat_id="c1",
        native_store=native_store.as_posix(),
        kind="primary",
    )

    try:
        monkeypatch.chdir(drifted_cwd)
        store = IdleStore(tmp_path / "idle")
        service = IdleService(
            store=store,
            config=MeridianConfig(),
            env={
                "HOME": home.as_posix(),
                "MERIDIAN_CHAT_ID": chat_id,
                "MERIDIAN_PROJECT_DIR": project.as_posix(),
                "MERIDIAN_SESSION_ROLE": "primary",
            },
            spawn_reader=lambda: (),
            detect_ttl=detect_ttl,
        )

        service.arm(harness="claude", session=session_id)

        state = store.read("claude", session_id)
        assert state is not None
        assert state.ttl_seconds == expected_ttl
    finally:
        session_store.stop_session(runtime_root, "c1")
