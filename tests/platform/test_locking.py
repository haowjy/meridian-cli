from __future__ import annotations

import threading
from pathlib import Path

from meridian.lib.platform.locking import lock_file
from tests.conftest import posix_only, windows_only


def test_lock_file_acquires_and_releases(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.lock"

    with lock_file(lock_path) as handle:
        assert not handle.closed

    assert handle.closed

    with lock_file(lock_path) as reacquired:
        assert not reacquired.closed


def test_lock_file_is_reentrant_with_nested_acquisition(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.lock"

    with lock_file(lock_path) as outer:
        with lock_file(lock_path) as inner:
            assert inner is outer
            assert not inner.closed
        assert not outer.closed

    assert outer.closed


def test_lock_file_blocks_other_threads_until_release(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.lock"
    first_has_lock = threading.Event()
    release_first = threading.Event()
    waiter_attempting = threading.Event()
    second_acquired = threading.Event()
    errors: list[BaseException] = []

    def _holder() -> None:
        try:
            with lock_file(lock_path):
                first_has_lock.set()
                release_first.wait()
        except BaseException as exc:  # pragma: no cover - failure path for thread handoff
            errors.append(exc)
            first_has_lock.set()
            release_first.set()

    def _waiter() -> None:
        try:
            if not first_has_lock.wait(timeout=2):
                raise AssertionError("holder did not acquire lock")
            waiter_attempting.set()
            with lock_file(lock_path):
                second_acquired.set()
        except BaseException as exc:  # pragma: no cover - failure path for thread handoff
            errors.append(exc)
            second_acquired.set()

    holder = threading.Thread(target=_holder)
    waiter = threading.Thread(target=_waiter)
    holder.start()
    waiter.start()

    assert first_has_lock.wait(timeout=2)
    assert waiter_attempting.wait(timeout=2)
    assert not second_acquired.is_set()

    release_first.set()
    assert second_acquired.wait(timeout=2)

    holder.join(timeout=2)
    waiter.join(timeout=2)

    assert errors == []


@windows_only
def test_lock_file_writes_lock_byte_on_windows(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.lock"

    with lock_file(lock_path):
        pass

    assert lock_path.read_bytes() == b"\0"


@posix_only
def test_lock_file_keeps_empty_lock_file_on_posix(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.lock"

    with lock_file(lock_path):
        pass

    assert lock_path.read_bytes() == b""
