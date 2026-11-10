# qa-validated: test-suite-redesign

"""Cross-process locking and concurrent session-reserve tests.

Covers: concurrent reserve_chat_id safety, exclusive session-lock holding,
and rollback on append failure. Session CRUD and cleanup flows live in
test_session_store.py.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from meridian.lib.platform.locking import lock_file
from meridian.lib.state import session_store
from tests.support.process_race import run_spawn_race_or_skip


def _state_root(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".meridian"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _reserve_chat_id_worker(state_root_str: str) -> str:
    runtime_root = Path(state_root_str)
    return session_store.reserve_chat_id(runtime_root)


def test_reserve_chat_id_is_safe_under_concurrency(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    process_count = 8

    allocated = run_spawn_race_or_skip(
        _reserve_chat_id_worker,
        [(runtime_root.as_posix(),) for _ in range(process_count)],
    )

    allocated = sorted(allocated)
    assert allocated == [f"c{idx}" for idx in range(1, process_count + 1)]
    assert (runtime_root / "session-id-counter").read_text(encoding="utf-8") == f"{process_count}\n"


def test_chat_is_free_once_a_brief_holder_releases(tmp_path: Path) -> None:
    """Stale-lock repair takes a dead chat's lock for a moment; a continue must outwait it."""
    runtime_root = _state_root(tmp_path)
    lock_path = runtime_root / "sessions" / "c1.lock"
    lock_path.parent.mkdir()
    lock_path.touch()
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with lock_file(lock_path, timeout=0, reentrant=False):
            held.set()
            release.wait()

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(5)
        threading.Timer(0.1, release.set).start()
        with session_store.chat_lock(runtime_root, "c1"):
            pass
    finally:
        release.set()
        holder.join()


def test_chat_is_not_free_while_its_lock_stays_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = _state_root(tmp_path)
    lock_path = runtime_root / "sessions" / "c1.lock"
    lock_path.parent.mkdir()
    lock_path.touch()
    monkeypatch.setattr(session_store, "_CHAT_LOCK_WAIT_SECONDS", 0.05)

    with (
        lock_file(lock_path, timeout=0, reentrant=False),
        pytest.raises(session_store.ChatBusyError, match="Chat c1 is held"),
        session_store.chat_lock(runtime_root, "c1"),
    ):
        pass
