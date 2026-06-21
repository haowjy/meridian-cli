# qa-validated: test-suite-redesign

"""Cross-process locking and concurrent session-reserve tests.

Covers: concurrent reserve_chat_id safety, exclusive session-lock holding,
and rollback on append failure. Session CRUD and cleanup flows live in
test_session_store.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.state import session_store
from tests.support.process_race import run_spawn_race_or_skip


def _state_root(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".meridian"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _reserve_chat_id_worker(state_root_str: str) -> str:
    runtime_root = Path(state_root_str)
    return session_store.reserve_chat_id(runtime_root)


def _can_acquire_lock_nonblocking_worker(lock_path_str: str) -> bool:
    lock_path = Path(lock_path_str)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        locked = session_store._try_lock_nonblocking(handle)
        if not locked:
            return False
        session_store._release_session_lock_handle(handle)
        return True


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


def test_start_session_acquires_lifetime_lock_before_appending_start_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = _state_root(tmp_path)
    original_append_event = session_store.append_event
    observed = {"checked": False}

    def _append_event_with_lock_check(*args: object, **kwargs: object) -> None:
        event = kwargs.get("event")
        if event is None and len(args) >= 3:
            event = args[2]
        if isinstance(event, session_store.SessionStartEvent):
            lock_path = runtime_root / "sessions" / f"{event.chat_id}.lock"
            held = run_spawn_race_or_skip(
                _can_acquire_lock_nonblocking_worker,
                [(lock_path.as_posix(),)],
            )
            assert held == [False]
            observed["checked"] = True
        original_append_event(*args, **kwargs)

    monkeypatch.setattr(session_store, "append_event", _append_event_with_lock_check)

    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="thread-ordered",
        model="gpt-5.4",
    )
    try:
        assert observed["checked"] is True
    finally:
        session_store.stop_session(runtime_root, chat_id)


def test_start_session_holds_exclusive_lock_across_processes(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="thread-exclusive",
        model="gpt-5.4",
    )
    lock_path = runtime_root / "sessions" / f"{chat_id}.lock"

    try:
        held = run_spawn_race_or_skip(
            _can_acquire_lock_nonblocking_worker,
            [(lock_path.as_posix(),)],
        )
        assert held == [False]
    finally:
        session_store.stop_session(runtime_root, chat_id)

    released = run_spawn_race_or_skip(
        _can_acquire_lock_nonblocking_worker,
        [(lock_path.as_posix(),)],
    )
    assert released == [True]


def test_start_session_rolls_back_lock_and_event_on_append_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = _state_root(tmp_path)
    chat_id = "c99"

    def _raise_append_error(*_: object, **__: object) -> None:
        raise RuntimeError("append failed")

    monkeypatch.setattr(session_store, "append_event", _raise_append_error)

    with pytest.raises(RuntimeError, match="append failed"):
        session_store.start_session(
            runtime_root,
            harness="codex",
            harness_session_id="thread-fail",
            model="gpt-5.4",
            chat_id=chat_id,
        )

    assert not (runtime_root / "sessions.jsonl").exists()
    assert not (runtime_root / "sessions" / f"{chat_id}.lease.json").exists()
    assert (
        session_store._session_lock_key(runtime_root, chat_id)
        not in session_store._SESSION_LOCK_HANDLES
    )

    lock_path = runtime_root / "sessions" / f"{chat_id}.lock"
    assert run_spawn_race_or_skip(
        _can_acquire_lock_nonblocking_worker,
        [(lock_path.as_posix(),)],
    ) == [True]
