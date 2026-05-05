from pathlib import Path

from meridian.lib.state import session_store, spawn_store


def test_spawn_record_update_accepts_claude_config_dir(tmp_path: Path) -> None:
    runtime_root = tmp_path / "state"
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p1",
        chat_id="c1",
        model="gpt",
        agent="coder",
        harness="claude",
        prompt="do it",
    )

    spawn_store.update_spawn(runtime_root, "p1", claude_config_dir="/tmp/claude-overlay")

    record = spawn_store.get_spawn(runtime_root, "p1")
    assert record is not None
    assert record.claude_config_dir == "/tmp/claude-overlay"


def test_session_record_start_and_update_accept_claude_config_dir(tmp_path: Path) -> None:
    runtime_root = tmp_path / "state"
    chat_id = session_store.start_session(
        runtime_root,
        harness="claude",
        harness_session_id="session-1",
        model="opus",
        chat_id="c1",
        claude_config_dir="/tmp/initial-claude",
    )

    record = session_store.get_session_record(runtime_root, chat_id)
    assert record is not None
    assert record.claude_config_dir == "/tmp/initial-claude"

    session_store.update_session_claude_config_dir(
        runtime_root,
        chat_id,
        "/tmp/updated-claude",
    )

    updated = session_store.get_session_record(runtime_root, chat_id)
    assert updated is not None
    assert updated.claude_config_dir == "/tmp/updated-claude"
