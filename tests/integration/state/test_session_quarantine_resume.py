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


def _generation_count(runtime_root: Path, chat_id: str) -> int:
    return sum(
        1
        for record in session_store.list_session_generations(runtime_root)
        if record.chat_id == chat_id
    )


def _resume_expecting_new_generation(runtime_root: Path, chat_id: str) -> None:
    """An allowed resume must actually publish a generation, not merely not raise."""
    before = _generation_count(runtime_root, chat_id)
    _resume(runtime_root, chat_id)
    assert _generation_count(runtime_root, chat_id) == before + 1


def test_unparseable_row_still_blocks_resume(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    chat_id = _stopped_chat(runtime_root)
    state_path = runtime_root / "spawns" / "p1" / "state.json"
    state_path.parent.mkdir(parents=True)
    atomic_write_text(state_path, '{"v": 4,')

    with pytest.raises(ValueError, match="Cannot establish current-chat ownership"):
        _resume(runtime_root, chat_id)


@pytest.mark.parametrize(
    ("row_chat", "resume_under_lock"),    [
        pytest.param("same", True, id="same-chat-held-lock"),
        pytest.param("same", False, id="same-chat-free-lock"),
        pytest.param("other", True, id="other-chat-held-lock"),
    ],
)
def test_quarantined_chat_row_ownership_decides_resume(
    tmp_path: Path,
    row_chat: str,
    resume_under_lock: bool,
) -> None:
    runtime_root = tmp_path / "runtime"
    chat_id = _stopped_chat(runtime_root)
    blocked = row_chat == "same" and resume_under_lock
    with LaunchSlot() as launch_lock:
        _write_v4_row(
            runtime_root,
            "p1",
            current_chat_id=chat_id if row_chat == "same" else "c-other",
            launch_lock=launch_lock,
        )
        if resume_under_lock:
            if blocked:
                with pytest.raises(ValueError, match="Cannot establish current-chat ownership"):
                    _resume(runtime_root, chat_id)
            else:
                _resume_expecting_new_generation(runtime_root, chat_id)
    if not resume_under_lock:
        _resume_expecting_new_generation(runtime_root, chat_id)


@pytest.mark.parametrize(
    "resume_under_lock",
    [True, False],
    ids=["held-lock", "free-lock"],
)
def test_quarantined_chatless_row_ownership_decides_resume(
    tmp_path: Path,
    resume_under_lock: bool,
) -> None:
    runtime_root = tmp_path / "runtime"
    chat_id = _stopped_chat(runtime_root)
    with LaunchSlot() as launch_lock:
        _write_v4_row(
            runtime_root,
            "p1",
            current_chat_id=None,
            launch_lock=launch_lock,
        )
        if resume_under_lock:
            with pytest.raises(ValueError, match="Cannot establish current-chat ownership"):
                _resume(runtime_root, chat_id)
    if not resume_under_lock:
        _resume_expecting_new_generation(runtime_root, chat_id)
