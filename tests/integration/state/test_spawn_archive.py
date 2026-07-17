"""Spawn archive overlay concurrency regressions."""

from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.spawn import archive as archive_module
from tests.conftest import posix_only

if TYPE_CHECKING:
    import pytest


def _archive(runtime_root: Path, spawn_id: str) -> None:
    archive_module.archive_spawn(runtime_root, spawn_id)


@posix_only
def test_concurrent_archives_preserve_both_spawn_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two processes must not overwrite each other's archive insertion."""
    context = multiprocessing.get_context("fork")
    both_reads_finished = context.Barrier(2)
    original_read = archive_module.read_archived_spawns

    def pause_after_read(runtime_root: Path) -> set[str]:
        archived = original_read(runtime_root)
        both_reads_finished.wait(timeout=5)
        return archived

    monkeypatch.setattr(archive_module, "read_archived_spawns", pause_after_read)

    processes = [
        context.Process(target=_archive, args=(tmp_path, spawn_id))
        for spawn_id in ("p1", "p2")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
        assert process.exitcode == 0

    assert original_read(tmp_path) == {"p1", "p2"}
