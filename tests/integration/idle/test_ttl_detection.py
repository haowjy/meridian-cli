"""Recorded native identity integration for idle cache-TTL detection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.config.models import MeridianConfig
from meridian.lib.harness.claude_idle import detect_ttl
from meridian.lib.idle.service import IdleService
from meridian.lib.state import session_store
from meridian.lib.state.idle_store import IdleStore
from meridian.lib.state.paths import resolve_project_runtime_root_for_write


@pytest.mark.integration
@pytest.mark.parametrize(("chat_id", "expected_ttl"), [("c1", 3600), ("missing", None)])
def test_claude_ttl_uses_recorded_store_for_successor_or_none_without_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chat_id: str,
    expected_ttl: int | None,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "launch-project"
    native_store = tmp_path / "claude-native-store"
    for directory in (home, project, native_store):
        directory.mkdir()
    monkeypatch.setenv("HOME", home.as_posix())
    monkeypatch.setenv("MERIDIAN_HOME", (home / ".meridian").as_posix())

    # `/clear` changes the native session ID while the managed process keeps the
    # launch chat in MERIDIAN_CHAT_ID. Claude files both IDs in the same store.
    entry_session_id = "entry-session"
    session_id = "successor-session"
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
        harness_session_id=entry_session_id,
        model="claude-opus-4-6",
        chat_id="c1",
        native_store=native_store.as_posix(),
        kind="primary",
    )

    try:
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
            detect_ttl=detect_ttl,
        )

        service.arm(harness="claude", session=session_id)

        state = store.read("claude", session_id)
        assert state is not None
        assert state.ttl_seconds == expected_ttl
    finally:
        session_store.stop_session(runtime_root, "c1")
