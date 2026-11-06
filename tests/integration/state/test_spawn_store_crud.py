# qa-validated: test-suite-redesign

"""CRUD operation tests for the spawn store.

Covers: start, get, list, update, next_id, reserve, record_launch_facts, and
attempt-exit and runner-exit metadata writes. Finalization and concurrency live in
test_spawn_store_finalize.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.bootstrap.runtime_state import ensure_runtime_dirs
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.spawn_store import (
    start_spawn,
)
from tests.conftest import posix_only


def _state_root(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".meridian"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _start_test_spawn(runtime_root: Path, *, spawn_id: str | None = None) -> str:
    return str(
        start_spawn(
            runtime_root,
            chat_id="c1",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="hello",
            spawn_id=spawn_id,
        ).spawn_id
    )


def test_runtime_startup_removes_only_abandoned_stages(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)
    final_spawn_dir = paths.spawns_dir / "p1"
    final_spawn_dir.mkdir(parents=True)
    (final_spawn_dir / "state.json").write_text("published\n", encoding="utf-8")
    abandoned_stage = paths.spawns_dir / ".staging" / "p2-1234-deadbeef"
    abandoned_stage.mkdir(parents=True)
    (abandoned_stage / "state.json").write_text("abandoned\n", encoding="utf-8")
    abandoned_file = paths.spawns_dir / ".staging" / "orphan.tmp"
    abandoned_file.write_text("abandoned\n", encoding="utf-8")

    ensure_runtime_dirs(runtime_root)

    assert (final_spawn_dir / "state.json").read_text(encoding="utf-8") == "published\n"
    assert list((paths.spawns_dir / ".staging").iterdir()) == []


@posix_only
def test_runtime_startup_does_not_follow_symlinked_staging_container(
    tmp_path: Path,
) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)
    paths.spawns_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    victim = outside_dir / "keep.txt"
    victim.write_text("keep\n", encoding="utf-8")
    staging_dir = paths.spawns_dir / ".staging"
    staging_dir.symlink_to(outside_dir, target_is_directory=True)

    ensure_runtime_dirs(runtime_root)

    assert victim.read_text(encoding="utf-8") == "keep\n"
    assert not staging_dir.is_symlink()


@posix_only
def test_start_spawn_rejects_symlinked_staging_container(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)
    paths.spawns_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    victim = outside_dir / "keep.txt"
    victim.write_text("keep\n", encoding="utf-8")
    (paths.spawns_dir / ".staging").symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(NotADirectoryError, match="staging container must be a real directory"):
        _start_test_spawn(runtime_root, spawn_id="p1")

    assert victim.read_text(encoding="utf-8") == "keep\n"
    assert not (paths.spawns_dir / "p1").exists()
