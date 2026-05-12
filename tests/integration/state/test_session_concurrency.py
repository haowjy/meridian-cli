# qa-validated: test-suite-redesign

"""Cross-process locking and concurrent session-reserve tests.

Covers: concurrent reserve_chat_id safety, lock-file retry on replacement,
lock-before-event ordering invariant, and rollback on append failure.
Session CRUD and cleanup flows live in test_session_store.py.
"""

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.state import session_store


def _state_root(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".meridian"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _spawn_queue_or_skip() -> tuple[Any, multiprocessing.Queue[Any]]:
    ctx = multiprocessing.get_context("spawn")
    try:
        queue: multiprocessing.Queue[Any] = ctx.Queue()
    except PermissionError as exc:
        pytest.skip(f"multiprocessing semaphore unavailable in this environment: {exc}")
    return ctx, queue


def _start_process_or_skip(proc: Any) -> None:
    try:
        proc.start()
    except PermissionError as exc:
        pytest.skip(f"multiprocessing semaphore unavailable in this environment: {exc}")


def _reserve_chat_id_worker(state_root_str: str, queue: multiprocessing.Queue[str]) -> None:
    runtime_root = Path(state_root_str)
    queue.put(session_store.reserve_chat_id(runtime_root))


def _can_acquire_lock_nonblocking_worker(
    lock_path_str: str, queue: multiprocessing.Queue[bool]
) -> None:
    lock_path = Path(lock_path_str)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        locked = session_store._try_lock_nonblocking(handle)
        if not locked:
            queue.put(False)
            return
        session_store._release_session_lock_handle(handle)
        queue.put(True)


def test_reserve_chat_id_is_safe_under_concurrency(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)

    process_count = 8
    ctx, queue = _spawn_queue_or_skip()
    procs = [
        ctx.Process(
            target=_reserve_chat_id_worker,
            args=(runtime_root.as_posix(), queue),
        )
        for _ in range(process_count)
    ]
    for proc in procs:
        _start_process_or_skip(proc)
    for proc in procs:
        proc.join(timeout=20)
        assert proc.exitcode == 0

    allocated = sorted(queue.get(timeout=5) for _ in range(process_count))
    assert allocated == [f"c{idx}" for idx in range(1, process_count + 1)]
    assert (runtime_root / "session-id-counter").read_text(encoding="utf-8") == f"{process_count}\n"


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl not available")
def test_acquire_session_lock_retries_when_lock_file_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = _state_root(tmp_path)
    lock_path = runtime_root / "sessions" / "c123.lock"
    real_flock = session_store.fcntl.flock
    observed = {"lock_ex_calls": 0, "replaced": False}

    def _flock_with_unlink(fd: int, operation: int) -> None:
        if operation == session_store.fcntl.LOCK_EX:
            observed["lock_ex_calls"] += 1
            if not observed["replaced"]:
                observed["replaced"] = True
                lock_path.unlink(missing_ok=True)
                lock_path.touch()
        real_flock(fd, operation)

    monkeypatch.setattr(session_store.fcntl, "flock", _flock_with_unlink)

    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="thread-replaced",
        model="gpt-5.4",
        chat_id="c123",
    )
    try:
        assert chat_id == "c123"
        assert observed["replaced"] is True
        assert observed["lock_ex_calls"] >= 2

        ctx, queue = _spawn_queue_or_skip()
        proc = ctx.Process(
            target=_can_acquire_lock_nonblocking_worker,
            args=(lock_path.as_posix(), queue),
        )
        _start_process_or_skip(proc)
        proc.join(timeout=20)
        assert proc.exitcode == 0
        assert queue.get(timeout=5) is False
    finally:
        session_store.stop_session(runtime_root, chat_id)


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
            ctx, queue = _spawn_queue_or_skip()
            lock_path = runtime_root / "sessions" / f"{event.chat_id}.lock"
            proc = ctx.Process(
                target=_can_acquire_lock_nonblocking_worker,
                args=(lock_path.as_posix(), queue),
            )
            _start_process_or_skip(proc)
            proc.join(timeout=20)
            assert proc.exitcode == 0
            assert queue.get(timeout=5) is False
            observed["checked"] = True
        original_append_event(*args, **kwargs)

    monkeypatch.setattr(session_store, "append_event", _append_event_with_lock_check)

    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="thread-ordered",
        model="gpt-5.4",
    )
    assert observed["checked"] is True

    session_store.stop_session(runtime_root, chat_id)


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

    ctx, queue = _spawn_queue_or_skip()
    lock_path = runtime_root / "sessions" / f"{chat_id}.lock"
    proc = ctx.Process(
        target=_can_acquire_lock_nonblocking_worker,
        args=(lock_path.as_posix(), queue),
    )
    _start_process_or_skip(proc)
    proc.join(timeout=20)
    assert proc.exitcode == 0
    assert queue.get(timeout=5) is True
