# qa-validated: test-suite-redesign
"""Doctor warning regression tests for high-risk repair and diagnostic paths."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from meridian.lib.ops import diag
from meridian.lib.ops import mars as mars_ops
from meridian.lib.ops.diag import DoctorInput, doctor_sync
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from tests.conftest import posix_only


def _create_project_root(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    return project_root


def _warning_by_code(result: diag.DoctorOutput, code: str) -> diag.DoctorWarning:
    return next(warning for warning in result.warnings if warning.code == code)


def _create_agent_skill_dirs(project_root: Path) -> None:
    (project_root / ".mars" / "agents").mkdir(parents=True, exist_ok=True)
    (project_root / ".mars" / "skills").mkdir(parents=True, exist_ok=True)


def _set_tree_mtime(path: Path, mtime: float) -> None:
    for current in (path, *path.rglob("*")):
        os.utime(current, (mtime, mtime))


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_active_spawn(project_root: Path, *, started_at: str | None = None) -> str:
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    return spawn_store.start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="running",
        started_at=started_at,
    )


def _run_doctor_without_upgrade_noise(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> diag.DoctorOutput:
    monkeypatch.setattr(
        diag,
        "check_upgrade_availability",
        lambda *_args, **_kwargs: mars_ops.UpgradeAvailability(),
    )
    return doctor_sync(DoctorInput(project_root=project_root.as_posix()))


def test_doctor_kill_orphans_skips_repair_when_depth_is_not_clearly_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    _seed_active_spawn(project_root)
    monkeypatch.setenv("_MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(
        diag,
        "check_upgrade_availability",
        lambda *_args, **_kwargs: mars_ops.UpgradeAvailability(),
    )

    result = doctor_sync(DoctorInput(project_root=project_root.as_posix(), kill_orphans=True))

    assert "orphan_runs" not in result.repaired


@pytest.mark.parametrize("depth_value", [None, "0"])
def test_doctor_kill_orphans_repairs_orphan_runs_when_depth_is_clearly_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    depth_value: str | None,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    spawn_id = _seed_active_spawn(project_root, started_at="2020-01-01T00:00:00Z")
    if depth_value is None:
        monkeypatch.delenv("_MERIDIAN_DEPTH", raising=False)
    else:
        monkeypatch.setenv("_MERIDIAN_DEPTH", depth_value)
    monkeypatch.setattr(
        diag,
        "check_upgrade_availability",
        lambda *_args, **_kwargs: mars_ops.UpgradeAvailability(),
    )

    result = doctor_sync(DoctorInput(project_root=project_root.as_posix(), kill_orphans=True))

    assert "orphan_runs" in result.repaired
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    assert spawn_store.get_spawn(runtime_root, spawn_id).status == "failed"
    assert result.killed_orphan_spawns == (spawn_id,)


def test_doctor_does_not_reconcile_orphans_without_kill_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    spawn_id = _seed_active_spawn(project_root, started_at="2020-01-01T00:00:00Z")
    monkeypatch.delenv("_MERIDIAN_DEPTH", raising=False)
    monkeypatch.setattr(
        diag,
        "check_upgrade_availability",
        lambda *_args, **_kwargs: mars_ops.UpgradeAvailability(),
    )

    result = doctor_sync(DoctorInput(project_root=project_root.as_posix()))

    assert "orphan_runs" not in result.repaired
    assert result.killed_orphan_spawns == ()
    warning = _warning_by_code(result, "live_active_spawns_remain")
    assert warning.payload == {"spawn_ids": [spawn_id]}
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    assert spawn_store.get_spawn(runtime_root, spawn_id).status == "running"


def test_doctor_live_active_warning_uses_post_repair_spawns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    stale_spawn_id = _seed_active_spawn(project_root, started_at="2020-01-01T00:00:00Z")
    live_spawn_id = _seed_active_spawn(project_root, started_at=datetime.now(UTC).isoformat())
    monkeypatch.delenv("_MERIDIAN_DEPTH", raising=False)
    monkeypatch.setattr(
        diag,
        "check_upgrade_availability",
        lambda *_args, **_kwargs: mars_ops.UpgradeAvailability(),
    )

    result = doctor_sync(DoctorInput(project_root=project_root.as_posix(), kill_orphans=True))

    assert "orphan_runs" in result.repaired
    warning = _warning_by_code(result, "live_active_spawns_remain")
    assert warning.payload == {"spawn_ids": [live_spawn_id]}
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    assert spawn_store.get_spawn(runtime_root, stale_spawn_id).status == "failed"


def test_doctor_prune_preserves_v2_state_dir_after_same_run_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    stale_spawn_id = _seed_active_spawn(project_root, started_at="2020-01-01T00:00:00Z")
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    stale_artifact_dir = runtime_root / "spawns" / stale_spawn_id
    _write_text(stale_artifact_dir / "history.jsonl", '{"event":"start"}\n')
    _write_text(stale_artifact_dir / "report.md", "done\n")
    _set_tree_mtime(stale_artifact_dir, 1_600_000_000.0)
    monkeypatch.delenv("_MERIDIAN_DEPTH", raising=False)
    monkeypatch.setattr(
        diag,
        "check_upgrade_availability",
        lambda *_args, **_kwargs: mars_ops.UpgradeAvailability(),
    )

    result = doctor_sync(
        DoctorInput(project_root=project_root.as_posix(), prune=True, kill_orphans=True)
    )

    assert "orphan_runs" in result.repaired
    assert result.pruned_spawn_artifacts == 0
    assert stale_artifact_dir.exists()
    assert spawn_store.get_spawn(runtime_root, stale_spawn_id) is not None


@posix_only
def test_doctor_reports_locks_removed_by_post_prune_gc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    artifact_dir = runtime_root / "spawns" / "p7"
    _write_text(artifact_dir / "report.md", "done\n")
    _set_tree_mtime(artifact_dir, 1_600_000_000.0)
    lock_path = runtime_root / "locks" / "spawns" / "p7.lock"
    _write_text(lock_path, "")
    monkeypatch.setattr(
        diag,
        "check_upgrade_availability",
        lambda *_args, **_kwargs: mars_ops.UpgradeAvailability(),
    )

    result = doctor_sync(DoctorInput(project_root=project_root.as_posix(), prune=True))

    assert result.pruned_spawn_artifacts == 1
    assert not lock_path.exists()
    assert result.lock_gc.files_removed > 0
    assert "orphaned_locks" in result.repaired


def test_doctor_reports_outdated_dependency_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    monkeypatch.setattr(
        diag,
        "check_upgrade_availability",
        lambda *_args, **_kwargs: mars_ops.UpgradeAvailability(
            within_constraint=("meridian-dev-workflow",),
            beyond_constraint=("meridian-base",),
        ),
    )

    result = doctor_sync(DoctorInput(project_root=project_root.as_posix()))

    outdated = _warning_by_code(result, "outdated_dependencies")
    assert outdated.payload == {
        "within_constraint": ["meridian-dev-workflow"],
        "beyond_constraint": ["meridian-base"],
    }
    assert "meridian mars upgrade" in outdated.message
    assert all(warning.code != "updates_check_failed" for warning in result.warnings)


def test_doctor_surfaces_workspace_unknown_and_missing_root_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    (project_root / "meridian.toml").write_text(
        '[workspace.docs]\npath = "./missing-root"\nnote = "kept"\n',
        encoding="utf-8",
    )
    (project_root / "meridian.local.toml").write_text(
        '[workspace.local]\npath = "./missing-local"\n',
        encoding="utf-8",
    )

    result = _run_doctor_without_upgrade_noise(project_root, monkeypatch)

    unknown = _warning_by_code(result, "workspace_unknown_key")
    assert unknown.payload == {"keys": ["workspace.docs.note"]}
    local_missing = _warning_by_code(result, "workspace_local_missing_root")
    assert local_missing.payload == {
        "name": "local",
        "path": (project_root / "missing-local").resolve().as_posix(),
    }
    missing = _warning_by_code(result, "workspace_missing_root")
    assert missing.payload == {
        "roots": [(project_root / "missing-root").resolve().as_posix()],
    }


def test_doctor_warns_when_legacy_worktree_temp_dir_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    (runtime_root / "worktree-temp").mkdir(parents=True, exist_ok=True)

    result = _run_doctor_without_upgrade_noise(project_root, monkeypatch)

    warning = _warning_by_code(result, "legacy_worktree_temp_dir")
    assert warning.payload == {"path": (runtime_root / "worktree-temp").as_posix()}
