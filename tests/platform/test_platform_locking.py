"""Cross-process lock behavior tests.

Classified platform: uses multiprocessing.Process to test cross-process
locking — real I/O + OS-level platform behavior. Moved from tests/unit/.

# qa-validated: test-suite-redesign
"""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

from meridian.lib.platform.locking import lock_file, try_lock_file
from tests.conftest import posix_only


def _try_in_forked_child(lock_path: Path, result: multiprocessing.Queue[bool]) -> None:
    with try_lock_file(lock_path) as handle:
        result.put(handle is not None)


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
