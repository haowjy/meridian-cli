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


def _write_quarantined_row(
    runtime_root: Path,
    spawn_id: str,
    *,
    current_chat_id: str | None,
    launch_lock: LaunchSlot,
    version: int = 3,
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
    payload: dict[str, object] = {"v": version, "id": spawn_id}
    if version == 3:
        payload["unknown_field"] = True  # Fails v3 validation without being a newer format.
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


_OWNERSHIP_ERROR = "Cannot establish current-chat ownership"
_NEWER_ERROR = "written by a newer Meridian"


@pytest.mark.parametrize(
    ("row_chat", "version", "resume_under_lock", "error"),
    [
        pytest.param("same", 3, True, _OWNERSHIP_ERROR, id="invalid-v3-same-chat-held-lock"),
        pytest.param("same", 3, False, None, id="invalid-v3-same-chat-free-lock"),
        pytest.param("other", 3, True, None, id="invalid-v3-other-chat-held-lock"),
        pytest.param(None, 3, True, _OWNERSHIP_ERROR, id="invalid-v3-chatless-held-lock"),
        pytest.param(None, 3, False, None, id="invalid-v3-chatless-free-lock"),
        pytest.param("same", 5, True, _NEWER_ERROR, id="newer-format-same-chat-held-lock"),
        pytest.param("same", 5, False, None, id="newer-format-same-chat-free-lock"),
        pytest.param(None, 5, True, None, id="newer-format-chatless-held-lock"),
    ],
)
def test_quarantined_row_ownership_decides_resume(
    tmp_path: Path,
    row_chat: str | None,
    version: int,
    resume_under_lock: bool,
    error: str | None,
) -> None:
    runtime_root = tmp_path / "runtime"
    chat_id = _stopped_chat(runtime_root)
    current_chat_id = {"same": chat_id, "other": "c-other", None: None}[row_chat]
    with LaunchSlot() as launch_lock:
        _write_quarantined_row(
            runtime_root,
            "p1",
            current_chat_id=current_chat_id,
            launch_lock=launch_lock,
            version=version,
        )
        if resume_under_lock:
            if error is not None:
                with pytest.raises(ValueError, match=error):
                    _resume(runtime_root, chat_id)
            else:
                _resume_expecting_new_generation(runtime_root, chat_id)
    if not resume_under_lock:
        _resume_expecting_new_generation(runtime_root, chat_id)
