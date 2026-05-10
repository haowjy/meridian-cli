"""Doctor warning regressions."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from meridian.lib.catalog import models as catalog_models
from meridian.lib.ops import diag
from meridian.lib.ops import mars as mars_ops
from meridian.lib.ops.config import ConfigShowInput, config_show_sync
from meridian.lib.ops.diag import DoctorInput, doctor_sync
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write


@pytest.fixture(autouse=True)
def _isolate_runtime_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MERIDIAN_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "user-home").as_posix())


def _create_project_root(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    return project_root


def _warning_by_code(result: diag.DoctorOutput, code: str) -> diag.DoctorWarning:
    return next(warning for warning in result.warnings if warning.code == code)


def _create_agent_skill_dirs(
    project_root: Path,
    *,
    create_agents_dir: bool = True,
    create_skills_dir: bool = True,
) -> None:
    if create_agents_dir:
        (project_root / ".mars" / "agents").mkdir(parents=True, exist_ok=True)
    if create_skills_dir:
        (project_root / ".mars" / "skills").mkdir(parents=True, exist_ok=True)


def _set_tree_mtime(path: Path, mtime: float) -> None:
    for current in (path, *path.rglob("*")):
        os.utime(current, (mtime, mtime))


def _set_path_mtime(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


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


@pytest.mark.parametrize(
    ("trigger", "expected_code", "expected_payload_keys"),
    [
        ("missing_skills_directories", "missing_skills_directories", ()),
        ("missing_agent_profile_directories", "missing_agent_profile_directories", ()),
        ("live_active_spawns_remain", "live_active_spawns_remain", ("spawn_ids",)),
    ],
)
def test_doctor_warning_shape_for_non_mars_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
    expected_code: str,
    expected_payload_keys: tuple[str, ...],
) -> None:
    project_root = _create_project_root(tmp_path)
    if trigger == "missing_skills_directories":
        _create_agent_skill_dirs(project_root, create_agents_dir=True, create_skills_dir=False)
    elif trigger == "missing_agent_profile_directories":
        _create_agent_skill_dirs(project_root, create_agents_dir=False, create_skills_dir=True)
    elif trigger == "live_active_spawns_remain":
        _create_agent_skill_dirs(project_root)
        _seed_active_spawn(project_root)
    else:  # pragma: no cover - defensive
        raise AssertionError(f"Unknown warning trigger: {trigger}")

    result = _run_doctor_without_upgrade_noise(project_root, monkeypatch)

    assert isinstance(result.warnings, tuple)
    matching = [warning for warning in result.warnings if warning.code == expected_code]
    assert len(matching) == 1, f"expected exactly one {expected_code} warning"
    warning = matching[0]
    assert warning.message.strip(), f"{expected_code} warning has empty message"
    if expected_payload_keys:
        assert warning.payload is not None
        assert set(expected_payload_keys).issubset(warning.payload.keys())
    else:
        assert warning.payload is None


@pytest.mark.parametrize("depth_value", ["1", "garbage", "1.5", "-1"])
def test_doctor_kill_orphans_skips_repair_when_depth_is_not_clearly_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    depth_value: str,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    _seed_active_spawn(project_root)
    monkeypatch.setenv("MERIDIAN_DEPTH", depth_value)
    monkeypatch.setattr(
        diag,
        "check_upgrade_availability",
        lambda *_args, **_kwargs: mars_ops.UpgradeAvailability(),
    )

    result = doctor_sync(DoctorInput(project_root=project_root.as_posix(), kill_orphans=True))

    assert "orphan_runs" not in result.repaired


@pytest.mark.parametrize("depth_value", [None, "", "0"])
def test_doctor_kill_orphans_repairs_orphan_runs_when_depth_is_clearly_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    depth_value: str | None,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    spawn_id = _seed_active_spawn(project_root, started_at="2020-01-01T00:00:00Z")
    if depth_value is None:
        monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
    else:
        monkeypatch.setenv("MERIDIAN_DEPTH", depth_value)
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
    monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
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
    monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
    monkeypatch.setattr(
        diag,
        "check_upgrade_availability",
        lambda *_args, **_kwargs: mars_ops.UpgradeAvailability(),
    )

    result = doctor_sync(DoctorInput(project_root=project_root.as_posix(), kill_orphans=True))

    assert "orphan_runs" in result.repaired
    warning = _warning_by_code(result, "live_active_spawns_remain")
    assert warning.payload == {"spawn_ids": [live_spawn_id]}
    assert all(warning.code != "active_spawns_present" for warning in result.warnings)
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
    monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
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


def test_doctor_reports_no_warnings_when_conditions_are_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)

    result = _run_doctor_without_upgrade_noise(project_root, monkeypatch)

    assert result.warnings == ()
    assert result.ok is True
    assert not (tmp_path / "user-home" / "doctor-cache.json").exists()


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
    assert "1 update available within your pinned constraint: meridian-dev-workflow." in (
        outdated.message
    )
    assert "Run `meridian mars upgrade` to apply." in outdated.message
    assert "1 newer version available beyond your pinned constraint: meridian-base." in (
        outdated.message
    )
    assert "Edit mars.toml to bump the version, then run `meridian mars sync`." in (
        outdated.message
    )
    assert all(warning.code != "updates_check_failed" for warning in result.warnings)


def test_doctor_reports_update_check_failure_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    monkeypatch.setattr(diag, "check_upgrade_availability", lambda *_args, **_kwargs: None)

    result = doctor_sync(DoctorInput(project_root=project_root.as_posix()))

    updates_check = _warning_by_code(result, "updates_check_failed")
    assert updates_check.message == (
        "Could not check for dependency updates (`meridian mars outdated --json` failed)."
    )
    assert updates_check.payload is None
    assert all(warning.code != "outdated_dependencies" for warning in result.warnings)


def test_doctor_warning_surface_matches_config_show_for_missing_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "missing-repo"
    shown = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    assert shown.warning is not None
    monkeypatch.setattr(
        diag,
        "check_upgrade_availability",
        lambda *_args, **_kwargs: mars_ops.UpgradeAvailability(),
    )

    result = doctor_sync(DoctorInput(project_root=project_root.as_posix()))

    assert shown.warning in {warning.message for warning in result.warnings}


def test_doctor_text_output_prefixes_warning_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    monkeypatch.setattr(diag, "check_upgrade_availability", lambda *_args, **_kwargs: None)

    result = doctor_sync(DoctorInput(project_root=project_root.as_posix()))

    text = result.format_text()
    assert "warning: updates_check_failed: Could not check for dependency updates" in text


def test_doctor_reports_telemetry_retention_stats_and_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    old_segment = runtime_root / "telemetry" / "123-0001.jsonl"
    _write_text(old_segment, '{"event":"old"}\n')
    _set_path_mtime(old_segment, 1_600_000_000.0)
    monkeypatch.setattr(
        diag,
        "check_upgrade_availability",
        lambda *_args, **_kwargs: mars_ops.UpgradeAvailability(),
    )

    result = doctor_sync(DoctorInput(project_root=project_root.as_posix()))

    assert result.telemetry_cleanup is not None
    assert result.telemetry_cleanup.total_segments == 1
    assert result.telemetry_cleanup.expired_segments == 1
    assert result.telemetry_cleanup.deleted_segments == 0
    warning = _warning_by_code(result, "stale_telemetry_segments")
    assert warning.payload == {
        "total_segments": 1,
        "total_bytes": old_segment.stat().st_size,
        "expired_segments": 1,
        "max_total_bytes": 100_000_000,
    }
    assert "--prune" in warning.message
    assert old_segment.exists()


def test_doctor_prune_deletes_expired_telemetry_segments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    old_segment = runtime_root / "telemetry" / "123-0001.jsonl"
    _write_text(old_segment, '{"event":"old"}\n')
    _set_path_mtime(old_segment, 1_600_000_000.0)
    monkeypatch.setattr(
        diag,
        "check_upgrade_availability",
        lambda *_args, **_kwargs: mars_ops.UpgradeAvailability(),
    )

    result = doctor_sync(DoctorInput(project_root=project_root.as_posix(), prune=True))

    assert result.telemetry_cleanup is not None
    assert result.telemetry_cleanup.total_segments == 1
    assert result.telemetry_cleanup.deleted_segments == 1
    assert result.telemetry_cleanup.deleted_bytes > 0
    assert "telemetry_segments" in result.repaired
    assert all(warning.code != "stale_telemetry_segments" for warning in result.warnings)
    assert not old_segment.exists()


def test_doctor_surfaces_workspace_invalid_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    (project_root / "meridian.toml").write_text("[workspace.bad]\n", encoding="utf-8")

    result = _run_doctor_without_upgrade_noise(project_root, monkeypatch)

    warning = _warning_by_code(result, "workspace_invalid")
    assert "Invalid workspace schema" in warning.message
    assert warning.payload == {"path": (project_root / "meridian.toml").resolve().as_posix()}


def test_doctor_surfaces_workspace_unknown_and_missing_root_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    (project_root / "meridian.toml").write_text(
        "[workspace.docs]\n"
        'path = "./missing-root"\n'
        'note = "kept"\n',
        encoding="utf-8",
    )
    (project_root / "meridian.local.toml").write_text(
        "[workspace.local]\n"
        'path = "./missing-local"\n',
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


def test_doctor_surfaces_named_workspace_local_missing_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    (project_root / "meridian.local.toml").write_text(
        "[workspace.local_missing]\n"
        'path = "./missing-local"\n',
        encoding="utf-8",
    )

    result = _run_doctor_without_upgrade_noise(project_root, monkeypatch)

    local_missing = _warning_by_code(result, "workspace_local_missing_root")
    assert local_missing.payload == {
        "name": "local_missing",
        "path": (project_root / "missing-local").resolve().as_posix(),
    }


# test_doctor_surfaces_workspace_unsupported_harness_for_codex was removed
# because Codex now supports --add-dir for workspace projection.


def test_doctor_skips_model_resolution_for_config_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _create_project_root(tmp_path)
    _create_agent_skill_dirs(project_root)
    monkeypatch.setenv("MERIDIAN_DEFAULT_MODEL", "gpt-5.4")
    monkeypatch.setattr(
        diag,
        "check_upgrade_availability",
        lambda *_args, **_kwargs: mars_ops.UpgradeAvailability(),
    )

    def _unexpected_resolve_model(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("doctor config surface should not call model resolution")

    monkeypatch.setattr(catalog_models, "resolve_model", _unexpected_resolve_model)

    result = doctor_sync(DoctorInput(project_root=project_root.as_posix()))

    assert result.project_root == project_root.as_posix()


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
    monkeypatch.setenv("MERIDIAN_DEPTH", "0")

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
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
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
