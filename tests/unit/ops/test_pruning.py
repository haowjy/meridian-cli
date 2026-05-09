from __future__ import annotations

import os
from pathlib import Path

import pytest

from meridian.lib.ops.pruning import (
    prune_orphan_project_dirs,
    prune_stale_spawn_artifacts,
    scan_orphan_project_dirs,
    scan_stale_spawn_artifacts,
)
from meridian.lib.state import spawn_store

_EPOCH_NOW = 2_000_000_000.0
_DAY = 24 * 60 * 60


@pytest.fixture(autouse=True)
def _isolate_meridian_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "user-home").as_posix())


def _set_tree_mtime(path: Path, mtime: float) -> None:
    for current in (path, *path.rglob("*")):
        os.utime(current, (mtime, mtime), follow_symlinks=False)


def _set_path_mtime(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime), follow_symlinks=False)


def _write_payload(path: Path, content: str = "payload") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_scan_orphan_project_dirs_respects_retention_days_semantics(tmp_path: Path) -> None:
    user_home = tmp_path / "user-home"
    projects_root = user_home / "projects"
    stale = projects_root / "stale-uuid"
    fresh = projects_root / "fresh-uuid"
    active = projects_root / "active-uuid"

    _write_payload(stale / "state.txt")
    _write_payload(fresh / "state.txt")
    active.mkdir(parents=True, exist_ok=True)
    spawn_store.start_spawn(
        active,
        chat_id="c1",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="active",
    )

    _set_tree_mtime(stale, _EPOCH_NOW - (40 * _DAY))
    _set_tree_mtime(fresh, _EPOCH_NOW - (1 * _DAY))
    _set_tree_mtime(active, _EPOCH_NOW - (40 * _DAY))

    stale_only = scan_orphan_project_dirs(user_home, 30, _EPOCH_NOW)
    aggressive = scan_orphan_project_dirs(user_home, 0, _EPOCH_NOW)
    never = scan_orphan_project_dirs(user_home, -1, _EPOCH_NOW)

    assert [item.uuid for item in stale_only] == ["stale-uuid"]
    assert {item.uuid for item in aggressive} == {"fresh-uuid", "stale-uuid"}
    assert never == []


def test_scan_stale_spawn_artifacts_respects_scope_and_retention_semantics(
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "user-home"
    current_root = user_home / "projects" / "current-uuid"
    other_root = user_home / "projects" / "other-uuid"
    stale_spawn = current_root / "spawns" / "p1"
    active_spawn = current_root / "spawns" / "p2"
    other_spawn = other_root / "spawns" / "p9"

    _write_payload(stale_spawn / "history.jsonl", '{"event":"start"}\n')
    _write_payload(active_spawn / "history.jsonl", '{"event":"start"}\n')
    _write_payload(other_spawn / "history.jsonl", '{"event":"start"}\n')
    _set_tree_mtime(stale_spawn, _EPOCH_NOW - (40 * _DAY))
    _set_tree_mtime(active_spawn, _EPOCH_NOW - (1 * _DAY))
    _set_tree_mtime(other_spawn, _EPOCH_NOW - (40 * _DAY))
    _set_path_mtime(current_root, _EPOCH_NOW - (1 * _DAY))
    _set_path_mtime(other_root, _EPOCH_NOW - (1 * _DAY))

    active_spawn_ids = {"p2"}
    stale_only = scan_stale_spawn_artifacts(current_root, 30, active_spawn_ids, _EPOCH_NOW)
    aggressive = scan_stale_spawn_artifacts(current_root, 0, active_spawn_ids, _EPOCH_NOW)
    never = scan_stale_spawn_artifacts(current_root, -1, active_spawn_ids, _EPOCH_NOW)

    assert [item.spawn_id for item in stale_only] == ["p1"]
    assert [item.spawn_id for item in aggressive] == ["p1"]
    assert never == []
    assert all(item.project_uuid == "current-uuid" for item in stale_only)


def test_prune_functions_are_idempotent(tmp_path: Path) -> None:
    user_home = tmp_path / "user-home"
    orphan_dir = user_home / "projects" / "orphan-uuid"
    current_root = user_home / "projects" / "current-uuid"
    stale_spawn = current_root / "spawns" / "p1"

    _write_payload(orphan_dir / "state.txt")
    _write_payload(stale_spawn / "history.jsonl", '{"event":"start"}\n')
    _set_tree_mtime(orphan_dir, _EPOCH_NOW - (40 * _DAY))
    _set_tree_mtime(stale_spawn, _EPOCH_NOW - (40 * _DAY))
    _set_path_mtime(current_root, _EPOCH_NOW - (1 * _DAY))

    orphans = scan_orphan_project_dirs(user_home, 30, _EPOCH_NOW)
    stale = scan_stale_spawn_artifacts(current_root, 30, set(), _EPOCH_NOW)

    assert prune_orphan_project_dirs(orphans) == 1
    assert prune_stale_spawn_artifacts(stale) == 1
    assert not orphan_dir.exists()
    assert not stale_spawn.exists()
    assert prune_orphan_project_dirs(orphans) == 0
    assert prune_stale_spawn_artifacts(stale) == 0
