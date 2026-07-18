# qa-validated: test-suite-redesign
"""Doctor prune and global cross-project cleanup tests.

Warning code regression tests live in test_diag_warnings.py.
"""

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from meridian.lib.ops import diag
from meridian.lib.ops import mars as mars_ops
from meridian.lib.ops.diag import DoctorInput, doctor_sync
from meridian.lib.ops.pruning import (
    OrphanProjectDir,
    prune_orphan_project_dirs,
    scan_orphan_project_dirs,
)
from meridian.lib.platform.locking import try_lock_file
from meridian.lib.state import session_store, work_store


def _create_project_root(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    return project_root


def _create_agent_skill_dirs(project_root: Path) -> None:
    (project_root / ".mars" / "agents").mkdir(parents=True, exist_ok=True)
    (project_root / ".mars" / "skills").mkdir(parents=True, exist_ok=True)


def _set_tree_mtime(path: Path, mtime: float) -> None:
    for current in (path, *path.rglob("*")):
        os.utime(current, (mtime, mtime))


def _set_path_mtime(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _hold_session(
    runtime_root: Path,
    ready: multiprocessing.Event,
    release: multiprocessing.Event,
) -> None:
    chat_id = session_store.start_session(
        runtime_root,
        harness="test",
        harness_session_id="h1",
        model="test",
        kind="primary",
    )
    ready.set()
    release.wait(5)
    session_store.stop_session(runtime_root, chat_id)


def _seed_pruning_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path, Path]:
    """Create a project root with stale artifacts and orphan dirs.

    Returns (project_root, current_spawn, orphan_root, other_spawn).
    """
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    user_home = tmp_path / "user-home"
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", (user_home / ".claude").as_posix())
    monkeypatch.setattr(
        diag,
        "check_upgrade_availability",
        lambda *_args, **_kwargs: mars_ops.UpgradeAvailability(),
    )

    current_uuid = "current-project-uuid"
    (project_root / ".meridian").mkdir(parents=True, exist_ok=True)
    (project_root / ".meridian" / "id").write_text(current_uuid, encoding="utf-8")

    current_root = user_home / "projects" / current_uuid
    current_spawn = current_root / "spawns" / "p1"
    _write_text(current_spawn / "history.jsonl", '{"event":"start"}\n')
    _set_tree_mtime(current_spawn, 1_600_000_000.0)
    _set_path_mtime(current_root, 1_900_000_000.0)

    orphan_root = user_home / "projects" / "orphan-uuid"
    _write_text(orphan_root / "state.txt", "orphan")
    _set_tree_mtime(orphan_root, 1_600_000_000.0)

    other_root = user_home / "projects" / "other-uuid"
    other_spawn = other_root / "spawns" / "p9"
    _write_text(other_spawn / "history.jsonl", '{"event":"start"}\n')
    _set_tree_mtime(other_spawn, 1_600_000_000.0)
    _set_path_mtime(other_root, 1_900_000_000.0)

    return project_root, current_spawn, orphan_root, other_spawn


def test_doctor_heals_legacy_work_item_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, _, _, _ = _seed_pruning_layout(tmp_path, monkeypatch)
    (project_root / "meridian.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                'path = "context/work"',
                'archive = "context/archive/work"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    project_state_dir = project_root / ".meridian"
    item = work_store.create_work_item(project_state_dir, "legacy-item")
    legacy_task_dir = tmp_path / "legacy-task-dir"
    legacy_task_dir.mkdir()
    status_path = work_store.work_scratch_dir(project_state_dir, item.name) / "__status.json"
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    payload["task_dir"] = None
    payload["worktree"]["path"] = legacy_task_dir.as_posix()
    status_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    result = doctor_sync(DoctorInput(project_root=project_root.as_posix()))

    healed = json.loads(status_path.read_text(encoding="utf-8"))
    assert healed["task_dir"] == legacy_task_dir.resolve().as_posix()
    assert "work_item_metadata" in result.repaired


def test_doctor_prune_only_prunes_current_project_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        project_root,
        current_spawn,
        orphan_root,
        other_spawn,
    ) = _seed_pruning_layout(tmp_path, monkeypatch)

    result = doctor_sync(DoctorInput(project_root=project_root.as_posix(), prune=True))

    assert result.pruned_orphan_dirs == 0
    assert result.pruned_spawn_artifacts == 1
    assert result.orphan_project_dirs == ()
    assert result.stale_spawn_artifacts and result.stale_spawn_artifacts[0].spawn_id == "p1"
    assert not current_spawn.exists()
    assert orphan_root.exists(), "orphan dir should NOT be pruned without --global"
    assert other_spawn.exists()


def test_doctor_local_mode_skips_cross_project_enumeration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    monkeypatch.setattr(
        diag,
        "check_upgrade_availability",
        lambda *_args, **_kwargs: mars_ops.UpgradeAvailability(),
    )
    monkeypatch.setattr(
        diag,
        "scan_orphan_project_dirs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("doctor local mode should not enumerate sibling projects")
        ),
    )

    result = doctor_sync(DoctorInput(project_root=project_root.as_posix(), global_=False))

    assert result.ok is True


def test_doctor_prune_with_global_also_prunes_global_orphan_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        project_root,
        current_spawn,
        orphan_root,
        other_spawn,
    ) = _seed_pruning_layout(tmp_path, monkeypatch)
    monkeypatch.setenv("_MERIDIAN_DEPTH", "0")

    result = doctor_sync(
        DoctorInput(project_root=project_root.as_posix(), prune=True, global_=True)
    )

    assert result.pruned_orphan_dirs == 1
    assert result.pruned_spawn_artifacts == 1
    assert result.orphan_project_dirs and result.orphan_project_dirs[0].uuid == "orphan-uuid"
    assert result.stale_spawn_artifacts and result.stale_spawn_artifacts[0].spawn_id == "p1"
    assert not orphan_root.exists()
    assert not current_spawn.exists()
    assert other_spawn.exists()
    assert result.ok is True


def test_doctor_global_requires_root_side_effect_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        project_root,
        current_spawn,
        orphan_root,
        other_spawn,
    ) = _seed_pruning_layout(tmp_path, monkeypatch)
    monkeypatch.setenv("_MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(
        diag,
        "scan_orphan_project_dirs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("global doctor guard should run before cross-project scan")
        ),
    )

    with pytest.raises(RuntimeError, match="Global doctor maintenance requires a root"):
        doctor_sync(DoctorInput(project_root=project_root.as_posix(), global_=True))

    assert current_spawn.exists()
    assert orphan_root.exists()
    assert other_spawn.exists()


def test_global_prune_cannot_unlink_live_session_lock_inode(tmp_path: Path) -> None:
    user_home = tmp_path / "user-home"
    runtime_root = user_home / "projects" / "project-uuid"
    runtime_root.mkdir(parents=True)
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    process = multiprocessing.Process(target=_hold_session, args=(runtime_root, ready, release))
    process.start()
    try:
        assert ready.wait(5)
        assert scan_orphan_project_dirs(user_home, retention_days=0, now=2_000_000_000.0) == []

        orphan = OrphanProjectDir(
            uuid=runtime_root.name,
            path=runtime_root.as_posix(),
            size_bytes=0,
            last_activity="1970-01-01T00:00:00+00:00",
            reason="stale",
        )
        assert prune_orphan_project_dirs([orphan]) == 0
        assert runtime_root.exists()
        with try_lock_file(runtime_root / "sessions" / "c1.lock", reentrant=False) as handle:
            assert handle is None
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)


def test_global_prune_rejects_lock_directory_supplied_directly(tmp_path: Path) -> None:
    lock_dir = tmp_path / "user-home" / "projects" / ".locks"
    lock_dir.mkdir(parents=True)
    orphan = OrphanProjectDir(
        uuid=".locks",
        path=lock_dir.as_posix(),
        size_bytes=0,
        last_activity="1970-01-01T00:00:00+00:00",
        reason="stale",
    )

    assert prune_orphan_project_dirs([orphan]) == 0
    assert lock_dir.is_dir()
