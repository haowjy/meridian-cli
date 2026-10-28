"""Session resume remains available beside quarantined spawn rows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.launch_lock import LaunchSlot


def _stopped_chat(runtime_root: Path) -> str:
    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="native-1",
        model="test",
        chat_id="c1",
    ).chat_id
    session_store.stop_session(runtime_root, chat_id)
    return chat_id


def _write_v4_row(
    runtime_root: Path,
    spawn_id: str,
    *,
    current_chat_id: str | None,
    launch_lock: LaunchSlot,
) -> None:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id=current_chat_id or "c9001",
        model="test",
        agent="",
        harness="codex",
        prompt="test",
        status="running",
        launch_lock=launch_lock,
    )
    state_path = runtime_root / "spawns" / spawn_id / "state.json"
    payload: dict[str, object] = {"v": 4, "id": spawn_id}
    if current_chat_id is not None:
        payload["current_chat_id"] = current_chat_id
    atomic_write_text(
        state_path,
        json.dumps(payload) + "\n",
    )


def _resume(runtime_root: Path, chat_id: str) -> None:
    resumed = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="native-1",
        model="test",
        chat_id=chat_id,
    )
    session_store.stop_session(runtime_root, resumed.chat_id)


def test_quarantined_row_does_not_block_resume(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    chat_id = _stopped_chat(runtime_root)
    with LaunchSlot() as launch_lock:
        _write_v4_row(
            runtime_root,
            "p1",
            current_chat_id="c-other",
            launch_lock=launch_lock,
        )
        _resume(runtime_root, chat_id)


def test_unparseable_row_still_blocks_resume(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    chat_id = _stopped_chat(runtime_root)
    state_path = runtime_root / "spawns" / "p1" / "state.json"
    state_path.parent.mkdir(parents=True)
    atomic_write_text(state_path, '{"v": 4,')

    with pytest.raises(ValueError, match="Cannot establish current-chat ownership"):
        _resume(runtime_root, chat_id)


def test_quarantined_row_for_same_chat_with_held_lock_blocks_resume(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    chat_id = _stopped_chat(runtime_root)
    with LaunchSlot() as launch_lock:
        _write_v4_row(
            runtime_root,
            "p1",
            current_chat_id=chat_id,
            launch_lock=launch_lock,
        )

        with pytest.raises(ValueError, match="Cannot establish current-chat ownership"):
            _resume(runtime_root, chat_id)


def test_quarantined_row_for_same_chat_with_free_lock_allows_resume(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    chat_id = _stopped_chat(runtime_root)
    with LaunchSlot() as launch_lock:
        _write_v4_row(
            runtime_root,
            "p1",
            current_chat_id=chat_id,
            launch_lock=launch_lock,
        )

    _resume(runtime_root, chat_id)


def test_quarantined_chatless_row_with_free_lock_allows_resume(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    chat_id = _stopped_chat(runtime_root)
    with LaunchSlot() as launch_lock:
        _write_v4_row(
            runtime_root,
            "p1",
            current_chat_id=None,
            launch_lock=launch_lock,
        )

    _resume(runtime_root, chat_id)


def test_quarantined_chatless_row_with_held_lock_blocks_resume(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    chat_id = _stopped_chat(runtime_root)
    with LaunchSlot() as launch_lock:
        _write_v4_row(
            runtime_root,
            "p1",
            current_chat_id=None,
            launch_lock=launch_lock,
        )

        with pytest.raises(ValueError, match="Cannot establish current-chat ownership"):
            _resume(runtime_root, chat_id)
