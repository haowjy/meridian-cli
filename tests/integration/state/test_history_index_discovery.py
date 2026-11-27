"""Catch-up finds spawns published by a writer that never marks this schema's queue."""

import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from meridian.lib.state import history_index
from meridian.lib.state.history_index import HistoryIndex
from meridian.lib.state.spawn_store import start_spawn


def _start(root: Path, spawn_id: str) -> None:
    start_spawn(
        root,
        spawn_id=spawn_id,
        chat_id="c1",
        model="m",
        agent="a",
        harness="codex",
        prompt="x",
    )


def _publish_unmarked(source_root: Path, root: Path, spawn_id: str) -> None:
    _start(source_root, spawn_id)
    stage = root / "spawns" / f".stage-{spawn_id}"
    shutil.copytree(source_root / "spawns" / spawn_id, stage)
    os.replace(stage, root / "spawns" / spawn_id)
    # Backdate past the timestamp-resolution window so the directory time is remembered.
    old = time.time() - 60 - len(spawn_id)
    os.utime(root / "spawns", (old, old))


def _indexed(index: HistoryIndex) -> list[str]:
    with sqlite3.connect(index.path) as db:
        return sorted(row[0] for row in db.execute("SELECT local_id FROM records"))


def test_unmarked_spawn_is_found_and_unchanged_directory_is_not_relisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    _start(root, "p1")
    index = HistoryIndex(root)
    assert index.rebuild().complete

    listings: list[object] = []
    real_listdir = os.listdir

    def counting(path: object = ".") -> list[str]:
        if str(path) == str(root / "spawns"):
            listings.append(path)
        return real_listdir(path)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(history_index.os, "listdir", counting)

    _publish_unmarked(tmp_path / "other", root, "p500")
    assert index.catch_up().complete
    assert _indexed(index) == ["p1", "p500"]

    listings.clear()
    assert index.catch_up().complete
    assert listings == []
