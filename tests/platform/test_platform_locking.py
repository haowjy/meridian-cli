"""Cross-process lock behavior tests.

Classified platform: uses multiprocessing.Process to test cross-process
locking — real I/O + OS-level platform behavior. Moved from tests/unit/.

# qa-validated: test-suite-redesign
"""

from __future__ import annotations

import multiprocessing
from pathlib import Path

from meridian.lib.platform.locking import lock_file, try_lock_file


def _hold_lock(
    lock_path: Path,
    ready: multiprocessing.Event,
    release: multiprocessing.Event,
) -> None:
    with lock_file(lock_path):
        ready.set()
        release.wait(5)


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
