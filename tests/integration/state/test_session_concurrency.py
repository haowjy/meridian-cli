# qa-validated: test-suite-redesign

"""Cross-process locking and concurrent session-reserve tests.

Covers: concurrent reserve_chat_id safety, exclusive session-lock holding,
and rollback on append failure. Session CRUD and cleanup flows live in
test_session_store.py.
"""

from __future__ import annotations

import multiprocessing
import os
import time
from pathlib import Path

import pytest

from meridian.lib.platform.locking import try_lock_file
from meridian.lib.state import session_store
from tests.conftest import posix_only
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
    with try_lock_file(lock_path, reentrant=False) as handle:
        return handle is not None


def _start_session_then_fork(
    runtime_root: Path,
    child_ready: multiprocessing.Event,
    child_exit: multiprocessing.Event,
) -> None:
    session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="thread-forked",
        model="gpt-5.4",
        chat_id="c1",
    )
    if os.fork() == 0:
        child_ready.set()
        child_exit.wait(5)
        os._exit(0)
    os._exit(0)


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


@posix_only
def test_fork_child_closes_inherited_direct_session_handles(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    runtime_root = _state_root(tmp_path)
    child_ready = context.Event()
    child_exit = context.Event()
    parent = context.Process(
        target=_start_session_then_fork,
        args=(runtime_root, child_ready, child_exit),
    )
    parent.start()
    try:
        assert child_ready.wait(5)
        deadline = time.monotonic() + 5
        while parent.exitcode is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert parent.exitcode == 0

        lock_paths = (
            runtime_root / "sessions" / "c1.lock",
            runtime_root.parent / ".locks" / f"{runtime_root.name}.lock",
        )
        for lock_path in lock_paths:
            with try_lock_file(lock_path, reentrant=False) as handle:
                assert handle is not None
    finally:
        child_exit.set()
        parent.join(5)
