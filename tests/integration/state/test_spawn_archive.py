"""Spawn archive overlay concurrency regressions."""

from __future__ import annotations

import threading
from pathlib import Path

from meridian.lib.spawn import archive as archive_module


def test_concurrent_archives_preserve_both_spawn_ids(
    tmp_path: Path,
) -> None:
    """A second mutator cannot read until the first releases the archive lock."""
    first_holds_lock = threading.Event()
    allow_first = threading.Event()
    second_started = threading.Event()
    second_entered_mutator = threading.Event()

    def first_mutator(archived: set[str]) -> bool:
        first_holds_lock.set()
        assert allow_first.wait(timeout=5)
        archived.add("p1")
        return True

    def second_mutator(archived: set[str]) -> bool:
        second_entered_mutator.set()
        archived.add("p2")
        return True

    first = threading.Thread(
        target=archive_module.mutate_archived_spawns,
        args=(tmp_path, first_mutator),
    )

    def run_second() -> None:
        second_started.set()
        archive_module.mutate_archived_spawns(tmp_path, second_mutator)

    second = threading.Thread(target=run_second)
    first.start()
    assert first_holds_lock.wait(timeout=5)
    second.start()
    assert second_started.wait(timeout=5)
    assert not second_entered_mutator.wait(timeout=0.2)

    allow_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert second_entered_mutator.is_set()
    assert archive_module.read_archived_spawns(tmp_path) == {"p1", "p2"}
