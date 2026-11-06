"""POSIX lock-inode identity revalidation."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import IO, Any

import pytest

from meridian.lib.platform.locking import (
    acquire_file_lock,
    release_file_lock,
    unlink_validated_lock,
)
from meridian.lib.state import lock_gc, spawn_store
from meridian.lib.state.spawn.repository import write_state_locked
from meridian.lib.state.spawn_aggregate import delete_published_spawn
from tests.conftest import posix_only


@posix_only
def test_acquire_file_lock_retries_when_lock_path_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "sessions" / "c123.lock"
    real_open = Path.open

    def _open_and_replace(self: Path, *args: Any, **kwargs: Any) -> Any:
        handle = real_open(self, *args, **kwargs)
        if self == lock_path and not replaced["done"]:
            replaced["done"] = True
            lock_path.unlink(missing_ok=True)
            lock_path.touch()
        return handle

    replaced = {"done": False}

    monkeypatch.setattr(Path, "open", _open_and_replace)

    handle = acquire_file_lock(lock_path)
    try:
        assert replaced["done"] is True
        handle_stat = os.fstat(handle.fileno())
        path_stat = lock_path.stat()
        assert (handle_stat.st_dev, handle_stat.st_ino) == (path_stat.st_dev, path_stat.st_ino)
    finally:
        release_file_lock(handle)


@posix_only
def test_unlink_validated_lock_race(tmp_path: Path) -> None:
    lock_path = tmp_path / "race.lock"
    gc_handle = acquire_file_lock(lock_path)
    gc_inode = os.fstat(gc_handle.fileno()).st_ino
    acquired = threading.Event()
    release_acquirer = threading.Event()
    acquirer_inode: list[int] = []

    def acquire_after_gc() -> None:
        handle = acquire_file_lock(lock_path)
        acquirer_inode.append(os.fstat(handle.fileno()).st_ino)
        acquired.set()
        assert release_acquirer.wait(timeout=5)
        release_file_lock(handle)

    contender = threading.Thread(target=acquire_after_gc)
    contender.start()
    time.sleep(0.05)
    assert not acquired.is_set()

    assert unlink_validated_lock(lock_path, gc_handle)
    assert not acquired.is_set()
    lock_path.touch()
    release_file_lock(gc_handle)
    try:
        assert acquired.wait(timeout=5)
        assert acquirer_inode != [gc_inode]
        path_stat = lock_path.stat()
        assert acquirer_inode == [path_stat.st_ino]

        with pytest.raises(TimeoutError):
            acquire_file_lock(lock_path, timeout=0)
    finally:
        release_acquirer.set()
        contender.join(timeout=5)
    assert not contender.is_alive()

    stale_handle = acquire_file_lock(lock_path)
    lock_path.unlink()
    lock_path.touch()
    assert not unlink_validated_lock(lock_path, stale_handle)
    assert lock_path.exists()
    release_file_lock(stale_handle)

    shared_handle = acquire_file_lock(lock_path, mode="shared")
    try:
        with pytest.raises(TimeoutError):
            acquire_file_lock(lock_path, timeout=0)
        assert lock_path.exists()
    finally:
        release_file_lock(shared_handle)


@posix_only
def test_sweeper_vs_republication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = "p1"
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c1",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="original",
    )
    spawn_store.update_spawn(runtime_root, spawn_id, desc="create spawn lock")
    assert delete_published_spawn(
        runtime_root,
        spawn_id,
        can_delete=lambda _record: True,
    )

    target_lock = runtime_root / "locks" / "spawns" / f"{spawn_id}.lock"
    unlinked = threading.Event()
    release_sweeper = threading.Event()
    original_unlink = lock_gc.unlink_validated_lock

    def pause_after_unlink(lock_path: Path, handle: IO[bytes]) -> bool:
        removed = original_unlink(lock_path, handle)
        if lock_path == target_lock and removed:
            unlinked.set()
            assert release_sweeper.wait(timeout=5)
        return removed

    monkeypatch.setattr(lock_gc, "unlink_validated_lock", pause_after_unlink)
    sweeper = threading.Thread(target=lock_gc.gc_orphaned_locks, args=(runtime_root,))
    sweeper.start()
    assert unlinked.wait(timeout=5)

    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c2",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="republished",
    )

    active_mutators = 0
    max_active_mutators = 0
    guard = threading.Lock()
    mutation_barrier = threading.Barrier(3)
    errors: list[BaseException] = []

    def mutate() -> None:
        nonlocal active_mutators, max_active_mutators
        mutation_barrier.wait(timeout=5)

        def observe_serialization(record: Any) -> Any:
            nonlocal active_mutators, max_active_mutators
            with guard:
                active_mutators += 1
                max_active_mutators = max(max_active_mutators, active_mutators)
            time.sleep(0.05)
            with guard:
                active_mutators -= 1
            return record

        try:
            write_state_locked(runtime_root / "spawns", spawn_id, observe_serialization)
        except BaseException as exc:
            errors.append(exc)

    mutators = [threading.Thread(target=mutate) for _ in range(2)]
    for mutator in mutators:
        mutator.start()
    mutation_barrier.wait(timeout=5)
    for mutator in mutators:
        mutator.join(timeout=5)

    release_sweeper.set()
    sweeper.join(timeout=5)
    assert not sweeper.is_alive()
    assert all(not mutator.is_alive() for mutator in mutators)
    assert errors == []
    assert max_active_mutators == 1
