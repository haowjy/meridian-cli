"""Cross-process lock behavior tests.

Classified platform: uses multiprocessing.Process to test cross-process
locking — real I/O + OS-level platform behavior. Moved from tests/unit/.

# qa-validated: test-suite-redesign
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.locking import lock_file, try_lock_file
from tests.conftest import posix_only


def _hold_lock(
    lock_path: Path,
    ready: multiprocessing.Event,
    release: multiprocessing.Event,
) -> None:
    with lock_file(lock_path):
        ready.set()
        release.wait(5)


def _try_in_forked_child(lock_path: Path, result: multiprocessing.Queue[bool]) -> None:
    with try_lock_file(lock_path) as handle:
        result.put(handle is not None)


def _fork_while_other_thread_holds_lock(
    lock_path: Path,
    child_ready: multiprocessing.Event,
    child_exit: multiprocessing.Event,
) -> None:
    lock_ready = threading.Event()

    def hold_lock() -> None:
        with lock_file(lock_path):
            lock_ready.set()
            threading.Event().wait()

    threading.Thread(target=hold_lock, daemon=True).start()
    assert lock_ready.wait(5)
    if os.fork() == 0:
        child_ready.set()
        child_exit.wait(5)
        os._exit(0)
    os._exit(0)


def _fork_inside_lock_context(
    lock_path: Path,
    child_cleaned_up: multiprocessing.Event,
    release_parent: multiprocessing.Event,
) -> None:
    child_pid = -1
    child_branch = False
    with lock_file(lock_path):
        child_pid = os.fork()
        if child_pid == 0:
            child_branch = True
        else:
            assert child_cleaned_up.wait(5)
            release_parent.wait(5)

    if child_branch:
        child_cleaned_up.set()
        release_parent.wait(5)
        os._exit(0)
    os.waitpid(child_pid, 0)


def test_try_lock_file_acquires_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.lock"

    with try_lock_file(lock_path) as handle:
        assert handle is not None
        assert not handle.closed


def test_try_lock_file_is_thread_local_reentrant(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.lock"

    with try_lock_file(lock_path) as outer:
        assert outer is not None
        with try_lock_file(lock_path) as inner:
            assert inner is outer


def test_try_lock_file_yields_none_when_other_process_holds_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.lock"
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    process = multiprocessing.Process(target=_hold_lock, args=(lock_path, ready, release))
    process.start()
    try:
        assert ready.wait(5)
        with try_lock_file(lock_path) as handle:
            assert handle is None
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)


@pytest.mark.skipif(
    IS_WINDOWS,
    reason="shared locks are advisory-only on Windows and do not exclude writers",
)
def test_shared_locks_coexist_but_exclude_independent_writer(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.lock"

    with lock_file(lock_path, mode="shared"):
        with try_lock_file(lock_path, mode="shared", reentrant=False) as reader:
            assert reader is not None
        with try_lock_file(lock_path, mode="exclusive", reentrant=False) as writer:
            assert writer is None


def test_lock_file_timeout_raises_on_contention(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.lock"

    with (
        lock_file(lock_path),
        pytest.raises(TimeoutError, match="exclusive lock"),
        lock_file(lock_path, timeout=0, reentrant=False),
    ):
        pass


def test_try_lock_file_does_not_swallow_body_oserror(tmp_path: Path) -> None:
    with (
        pytest.raises(OSError, match="body failure"),
        try_lock_file(tmp_path / "state.lock"),
    ):
        raise OSError("body failure")


@posix_only
def test_reentrant_registry_is_cleared_after_fork(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    lock_path = tmp_path / "state.lock"
    result = context.Queue()

    with lock_file(lock_path):
        process = context.Process(target=_try_in_forked_child, args=(lock_path, result))
        process.start()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
        assert process.exitcode == 0
        assert result.get(timeout=1) is False


@posix_only
def test_fork_child_closes_lock_held_by_other_parent_thread(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    lock_path = tmp_path / "state.lock"
    child_ready = context.Event()
    child_exit = context.Event()
    parent = context.Process(
        target=_fork_while_other_thread_holds_lock,
        args=(lock_path, child_ready, child_exit),
    )
    parent.start()
    try:
        assert child_ready.wait(5)
        deadline = time.monotonic() + 5
        while parent.exitcode is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert parent.exitcode == 0
        with try_lock_file(lock_path, reentrant=False) as handle:
            assert handle is not None
    finally:
        child_exit.set()
        parent.join(5)


@posix_only
def test_fork_child_context_cleanup_does_not_unlock_parent(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    lock_path = tmp_path / "state.lock"
    child_cleaned_up = context.Event()
    release_parent = context.Event()
    parent = context.Process(
        target=_fork_inside_lock_context,
        args=(lock_path, child_cleaned_up, release_parent),
    )
    parent.start()
    try:
        assert child_cleaned_up.wait(5)
        with try_lock_file(lock_path, reentrant=False) as handle:
            assert handle is None
    finally:
        release_parent.set()
        parent.join(5)
        if parent.is_alive():
            parent.terminate()
            parent.join(5)
