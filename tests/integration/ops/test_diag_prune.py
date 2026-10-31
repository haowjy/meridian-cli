# qa-validated: test-suite-redesign
"""Doctor prune and global cross-project cleanup tests.

Warning code regression tests live in test_diag_warnings.py.
"""

import json
import multiprocessing
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from meridian.lib.ops import diag
from meridian.lib.ops import mars as mars_ops
from meridian.lib.ops.diag import DoctorInput, doctor_sync
from meridian.lib.ops.pruning import (
    AdHocProjectDir,
    OrphanProjectDir,
    prune_adhoc_project_dirs,
    prune_orphan_project_dirs,
    scan_adhoc_project_dirs,
    scan_orphan_project_dirs,
)
from meridian.lib.platform.locking import try_lock_file
from meridian.lib.state import session_store, work_repository, work_store
from meridian.lib.state.project_identity import ensure_project_identity
from meridian.lib.state.user_paths import get_project_home


def _create_project_root(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    return project_root


def _create_agent_skill_dirs(project_root: Path) -> None:
    (project_root / "mars.toml").write_text("[settings]\n", encoding="utf-8")
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


def _seed_adhoc_root(
    user_home: Path,
    project_id: str,
    source_path: Path,
    *,
    mtime: float,
) -> Path:
    runtime_root = user_home / "projects" / project_id
    _write_text(
        runtime_root / "origin.json",
        json.dumps(
            {
                "path": source_path.as_posix(),
                "created_at": datetime.fromtimestamp(mtime, tz=UTC).isoformat(),
            }
        ),
    )
    _set_tree_mtime(runtime_root, mtime)
    return runtime_root


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
    ).chat_id
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
    monkeypatch.setenv("_MERIDIAN_DEPTH", "0")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", (user_home / ".claude").as_posix())
    monkeypatch.setattr(
        diag,
        "check_upgrade_availability",
        lambda *_args, **_kwargs: mars_ops.UpgradeAvailability(),
    )

    current_uuid = "current-project-uuid"
    (project_root / "meridian.toml").write_text(
        f'[project]\nid = "{current_uuid}"\n', encoding="utf-8"
    )

    current_root = user_home / "projects" / current_uuid
    current_spawn = current_root / "spawns" / "p1"
    _write_text(current_spawn / "history.jsonl", '{"event":"start"}\n')
    _write_text(
        current_root / "artifacts" / "p1" / "history.jsonl",
        '{"event":"legacy mirror"}\n',
    )
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


def test_doctor_repairs_malformed_work_item_metadata(
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
                "[project]",
                'id = "current-project-uuid"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    project_state_dir = project_root / ".meridian"
    item = work_repository.create_work_item(project_state_dir, "repair-item")
    status_path = work_store.work_scratch_dir(project_state_dir, item.name) / "__status.json"
    status_path.write_text("not json", encoding="utf-8")

    result = doctor_sync(DoctorInput(project_root=project_root.as_posix()))

    repaired = json.loads(status_path.read_text(encoding="utf-8"))
    assert repaired["status"] == "open"
    assert repaired["archived_at"] is None
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
    assert not (current_spawn.parent.parent / "artifacts" / "p1").exists()
    assert orphan_root.exists(), "orphan dir should NOT be pruned without --global"
    assert other_spawn.exists()


def test_doctor_artifact_retention_preserves_quarantine_and_prunes_stale_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, stale_spawn, _, _ = _seed_pruning_layout(tmp_path, monkeypatch)
    (project_root / "meridian.toml").write_text(
        '[project]\nid = "current-project-uuid"\n', encoding="utf-8"
    )
    runtime_root = stale_spawn.parent.parent
    quarantined_spawn = runtime_root / "spawns" / "p2"
    _write_text(quarantined_spawn / "state.json", "{}\n")
    _set_tree_mtime(quarantined_spawn, 1_600_000_000.0)

    result = doctor_sync(DoctorInput(project_root=project_root.as_posix(), prune=True))

    assert result.pruned_spawn_artifacts == 1
    assert not stale_spawn.exists()
    assert quarantined_spawn.exists()


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


def test_quarantine_only_project_is_not_pruned(tmp_path: Path) -> None:
    user_home = tmp_path / "user-home"
    runtime_root = user_home / "projects" / "project-uuid"
    quarantined_spawn = runtime_root / "spawns" / "p1"
    _write_text(quarantined_spawn / "state.json", "{}\n")
    _set_tree_mtime(runtime_root, 1_600_000_000.0)

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


def test_project_retention_scans_own_disjoint_namespaces(tmp_path: Path) -> None:
    user_home = tmp_path / "user-home"
    source_path = tmp_path / "bare"
    source_path.mkdir()
    adhoc_root = _seed_adhoc_root(
        user_home,
        "adhoc.bare.0123456789ab",
        source_path,
        mtime=1_600_000_000.0,
    )
    committed_root = user_home / "projects" / "committed-id"
    _write_text(committed_root / "state.txt", "old\n")
    _set_tree_mtime(committed_root, 1_600_000_000.0)
    origin_mtime = (adhoc_root / "origin.json").stat().st_mtime_ns

    orphans = scan_orphan_project_dirs(user_home, retention_days=30, now=2_000_000_000.0)
    adhoc = scan_adhoc_project_dirs(user_home, retention_days=30, now=2_000_000_000.0)

    assert [item.uuid for item in orphans] == ["committed-id"]
    assert [item.project_id for item in adhoc] == [adhoc_root.name]
    assert (adhoc_root / "origin.json").stat().st_mtime_ns == origin_mtime


def test_adhoc_scan_uses_session_record_activity_not_origin_mtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 10, 14, tzinfo=UTC)
    user_home = tmp_path / "user-home"
    source_path = tmp_path / "bare"
    source_path.mkdir()
    runtime_root = _seed_adhoc_root(
        user_home,
        "adhoc.bare.1123456789ab",
        source_path,
        mtime=(now - timedelta(days=40)).timestamp(),
    )
    ended_yesterday = (now - timedelta(days=1)).isoformat()
    monkeypatch.setattr(session_store, "utc_now_iso", lambda: ended_yesterday)
    chat_id = session_store.start_session(
        runtime_root,
        harness="test",
        harness_session_id="session-1",
        model="test",
        kind="primary",
    ).chat_id
    session_store.stop_session(runtime_root, chat_id)
    origin_mtime = (runtime_root / "origin.json").stat().st_mtime_ns
    sessions_mtime = (runtime_root / "sessions.jsonl").stat().st_mtime_ns

    candidates = scan_adhoc_project_dirs(
        user_home,
        retention_days=30,
        now=now.timestamp(),
    )
    assert len(candidates) == 1
    assert not candidates[0].prunable
    assert (runtime_root / "origin.json").stat().st_mtime_ns == origin_mtime
    assert (runtime_root / "sessions.jsonl").stat().st_mtime_ns == sessions_mtime


def test_adhoc_retention_zero_has_touch_interval_floor(tmp_path: Path) -> None:
    now = 2_000_000_000.0
    user_home = tmp_path / "user-home"
    recent_path = tmp_path / "recent"
    recent_path.mkdir()
    recent_root = _seed_adhoc_root(
        user_home,
        "adhoc.recent.0123456789ab",
        recent_path,
        mtime=now - (10 * 60),
    )
    old_path = tmp_path / "old"
    old_path.mkdir()
    old_root = _seed_adhoc_root(
        user_home,
        "adhoc.old.0123456789ab",
        old_path,
        mtime=now - (2 * 60 * 60),
    )

    candidates = scan_adhoc_project_dirs(user_home, retention_days=0, now=now)

    assert [candidate.project_id for candidate in candidates] == [old_root.name, recent_root.name]
    assert {candidate.project_id: candidate.prunable for candidate in candidates} == {
        old_root.name: True,
        recent_root.name: False,
    }


def test_adhoc_retention_zero_counts_recent_context_activity(tmp_path: Path) -> None:
    now = 2_000_000_000.0
    user_home = tmp_path / "user-home"
    source_path = tmp_path / "bare"
    source_path.mkdir()
    runtime_root = _seed_adhoc_root(
        user_home,
        "adhoc.bare.1123456789ab",
        source_path,
        mtime=now - 3597 - 5,
    )
    context_root = user_home / "context" / runtime_root.name
    work_repository.create_work_item(context_root, "fresh-item")
    _set_tree_mtime(context_root, now - 5)

    candidates = scan_adhoc_project_dirs(user_home, retention_days=0, now=now)

    assert len(candidates) == 1
    assert not candidates[0].prunable
    assert runtime_root.is_dir()
    assert context_root.is_dir()


@pytest.mark.parametrize(
    ("path_state", "expected_reason"),
    [("gone", "adhoc-path-gone"), ("marker", "adhoc-shadowed")],
)
def test_adhoc_gone_and_shadowed_paths_use_shorter_retention(
    tmp_path: Path,
    path_state: str,
    expected_reason: str,
) -> None:
    now = 2_000_000_000.0
    user_home = tmp_path / "user-home"
    source_path = tmp_path / path_state
    source_path.mkdir()
    runtime_root = _seed_adhoc_root(
        user_home,
        f"adhoc.{path_state}.2123456789ab",
        source_path,
        mtime=now - (4 * 24 * 60 * 60),
    )
    if path_state == "gone":
        source_path.rmdir()
    else:
        (source_path / "meridian.toml").write_text(
            '[project]\nid = "committed"\n', encoding="utf-8"
        )

    candidates = scan_adhoc_project_dirs(user_home, retention_days=3, now=now)

    assert len(candidates) == 1
    assert candidates[0].project_id == runtime_root.name
    assert candidates[0].path == source_path.as_posix()
    assert candidates[0].reason == expected_reason
    assert candidates[0].prunable


def test_adhoc_scan_without_origin_falls_back_to_tree_activity(tmp_path: Path) -> None:
    now = 2_000_000_000.0
    user_home = tmp_path / "user-home"
    runtime_root = user_home / "projects" / "adhoc.crashed.6123456789ab"
    _write_text(runtime_root / "state.txt", "crash residue\n")
    _set_tree_mtime(runtime_root, now - (8 * 24 * 60 * 60))

    candidates = scan_adhoc_project_dirs(user_home, retention_days=30, now=now)

    assert len(candidates) == 1
    assert candidates[0].path is None
    assert candidates[0].reason == "adhoc-path-gone"
    assert candidates[0].prunable
    assert (
        candidates[0].last_activity
        == datetime.fromtimestamp(now - (8 * 24 * 60 * 60), tz=UTC).isoformat()
    )


def test_adhoc_prune_removes_context_and_rechecks_activity_under_lock(tmp_path: Path) -> None:
    now = 2_000_000_000.0
    user_home = tmp_path / "user-home"
    source_path = tmp_path / "bare"
    source_path.mkdir()
    runtime_root = _seed_adhoc_root(
        user_home,
        "adhoc.bare.3123456789ab",
        source_path,
        mtime=1_600_000_000.0,
    )
    context_root = user_home / "context" / runtime_root.name
    _write_text(context_root / "work" / "old.md", "old context\n")
    candidates = scan_adhoc_project_dirs(user_home, retention_days=30, now=now)
    assert len(candidates) == 1

    os.utime(runtime_root / "origin.json", (now, now))
    assert prune_adhoc_project_dirs(candidates, retention_days=30, now=now) == 0
    assert runtime_root.is_dir()
    assert context_root.is_dir()

    old = 1_600_000_000.0
    os.utime(runtime_root / "origin.json", (old, old))
    refreshed = scan_adhoc_project_dirs(user_home, retention_days=30, now=now)
    assert prune_adhoc_project_dirs(refreshed, retention_days=30, now=now) == 1
    assert not runtime_root.exists()
    assert not context_root.exists()


def test_adhoc_prune_rejects_committed_candidate(tmp_path: Path) -> None:
    runtime_root = tmp_path / "user-home" / "projects" / "committed-id"
    runtime_root.mkdir(parents=True)
    candidate = AdHocProjectDir(
        project_id="committed-id",
        path=None,
        runtime_root=runtime_root.as_posix(),
        context_root=(tmp_path / "user-home" / "context" / "committed-id").as_posix(),
        size_bytes=0,
        last_activity="1970-01-01T00:00:00+00:00",
        reason="adhoc-path-gone",
        prunable=True,
    )

    assert prune_adhoc_project_dirs([candidate], retention_days=0, now=2_000_000_000.0) == 0
    assert runtime_root.is_dir()


def test_doctor_global_reports_and_prunes_shadowed_adhoc_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 2_000_000_000.0
    user_home = tmp_path / "user-home"
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    monkeypatch.setenv("_MERIDIAN_DEPTH", "0")
    monkeypatch.setattr(diag.time, "time", lambda: now)
    project_root = _create_project_root(tmp_path)
    old_shadowed_path = tmp_path / "initialized-eight-days-ago"
    old_shadowed_path.mkdir()
    old_runtime_root = _seed_adhoc_root(
        user_home,
        "adhoc.old-shadowed.4123456789ab",
        old_shadowed_path,
        mtime=now - (8 * 24 * 60 * 60),
    )
    (old_shadowed_path / "meridian.toml").write_text(
        '[project]\nid = "fresh-id"\n', encoding="utf-8"
    )
    old_context_root = user_home / "context" / old_runtime_root.name
    _write_text(old_context_root / "work" / "old.md", "old context\n")
    _set_tree_mtime(old_context_root, now - (8 * 24 * 60 * 60))

    fresh_shadowed_path = tmp_path / "initialized-just-now"
    fresh_shadowed_path.mkdir()
    fresh_runtime_root = _seed_adhoc_root(
        user_home,
        "adhoc.fresh-shadowed.5123456789ab",
        fresh_shadowed_path,
        mtime=now - 5,
    )
    (fresh_shadowed_path / "meridian.toml").write_text(
        '[project]\nid = "another-fresh-id"\n', encoding="utf-8"
    )
    origin_mtimes = {
        root.name: (root / "origin.json").stat().st_mtime_ns
        for root in (fresh_runtime_root, old_runtime_root)
    }

    report = doctor_sync(DoctorInput(project_root=project_root.as_posix(), global_=True))

    assert len(report.adhoc_project_dirs) == 2
    by_id = {candidate.project_id: candidate for candidate in report.adhoc_project_dirs}
    fresh = by_id[fresh_runtime_root.name]
    old = by_id[old_runtime_root.name]
    assert fresh.path == fresh_shadowed_path.as_posix()
    assert fresh.reason == "adhoc-shadowed"
    assert not fresh.prunable
    assert old.path == old_shadowed_path.as_posix()
    assert old.reason == "adhoc-shadowed"
    assert old.prunable

    text = report.format_text()
    assert f"adhoc_project: {fresh.project_id}" in text
    assert f"  origin_path: {fresh_shadowed_path}" in text
    assert "  reason: adhoc-shadowed" in text
    assert f"  last_activity: {fresh.last_activity}" in text
    assert f"  size_bytes: {fresh.size_bytes}" in text
    assert "  prunable: no" in text
    assert "  prunable: yes" in text

    payload = report.model_dump(mode="json")
    json_by_id = {item["project_id"]: item for item in payload["adhoc_project_dirs"]}
    assert json_by_id[fresh_runtime_root.name]["prunable"] is False
    assert json_by_id[old_runtime_root.name]["prunable"] is True
    assert {
        root.name: (root / "origin.json").stat().st_mtime_ns
        for root in (fresh_runtime_root, old_runtime_root)
    } == origin_mtimes

    pruned = doctor_sync(
        DoctorInput(project_root=project_root.as_posix(), global_=True, prune=True)
    )
    assert pruned.pruned_adhoc_dirs == 1
    assert fresh_runtime_root.is_dir()
    assert not old_runtime_root.exists()
    assert not old_context_root.exists()


def test_doctor_global_lists_non_quiescent_adhoc_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 2_000_000_000.0
    user_home = tmp_path / "user-home"
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    monkeypatch.setenv("_MERIDIAN_DEPTH", "0")
    monkeypatch.setattr(diag.time, "time", lambda: now)
    project_root = _create_project_root(tmp_path)
    source_path = tmp_path / "active-bare-dir"
    source_path.mkdir()
    runtime_root = _seed_adhoc_root(
        user_home,
        "adhoc.active.6123456789ab",
        source_path,
        mtime=now - (40 * 24 * 60 * 60),
    )
    chat_id = session_store.start_session(
        runtime_root,
        harness="test",
        harness_session_id="active-session",
        model="test",
        kind="primary",
    ).chat_id

    try:
        report = doctor_sync(
            DoctorInput(project_root=project_root.as_posix(), global_=True, prune=True)
        )
        assert len(report.adhoc_project_dirs) == 1
        assert report.adhoc_project_dirs[0].project_id == runtime_root.name
        assert not report.adhoc_project_dirs[0].prunable
        assert report.pruned_adhoc_dirs == 0
        assert runtime_root.is_dir()
    finally:
        session_store.stop_session(runtime_root, chat_id)


def test_doctor_adhoc_retention_ignores_cwd_project_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 2_000_000_000.0
    user_home = tmp_path / "user-home"
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    monkeypatch.setenv("_MERIDIAN_DEPTH", "0")
    monkeypatch.setattr(diag.time, "time", lambda: now)
    project_root = _create_project_root(tmp_path)
    (project_root / "meridian.toml").write_text(
        '[state]\nadhoc_retention_days = 0\n\n[project]\nid = "doctor-project"\n',
        encoding="utf-8",
    )
    source_path = tmp_path / "still-bare"
    source_path.mkdir()
    _seed_adhoc_root(
        user_home,
        "adhoc.still-bare.5123456789ab",
        source_path,
        mtime=now - (10 * 24 * 60 * 60),
    )

    project_value_ignored = doctor_sync(
        DoctorInput(project_root=project_root.as_posix(), global_=True)
    )
    assert len(project_value_ignored.adhoc_project_dirs) == 1
    assert not project_value_ignored.adhoc_project_dirs[0].prunable

    monkeypatch.setenv("MERIDIAN_STATE_ADHOC_RETENTION_DAYS", "0")
    env_value_applied = doctor_sync(DoctorInput(project_root=project_root.as_posix(), global_=True))
    assert len(env_value_applied.adhoc_project_dirs) == 1
    assert env_value_applied.adhoc_project_dirs[0].prunable


def test_doctor_global_prune_does_not_recreate_current_adhoc_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 2_000_000_000.0
    user_home = tmp_path / "user-home"
    project_root = _create_project_root(tmp_path)
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    monkeypatch.setenv("_MERIDIAN_DEPTH", "0")
    monkeypatch.setenv("MERIDIAN_STATE_ADHOC_RETENTION_DAYS", "0")
    monkeypatch.setattr(diag.time, "time", lambda: now)
    identity = ensure_project_identity(project_root)
    runtime_root = get_project_home(str(identity.id))
    old = now - (2 * 60 * 60)
    os.utime(runtime_root / "origin.json", (old, old))
    context_root = user_home / "context" / str(identity.id)
    _write_text(context_root / "work" / "old.md", "old context\n")

    result = doctor_sync(
        DoctorInput(project_root=project_root.as_posix(), global_=True, prune=True)
    )

    assert result.pruned_adhoc_dirs == 1
    assert not runtime_root.exists()
    assert not context_root.exists()
