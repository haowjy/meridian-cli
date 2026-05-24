"""spawn_create_sync behavior — dry-run resolution, telemetry, and goal preview.

Query/list/cancel/wait tests live in test_spawn_api_query.py.

# qa-validated: test-suite-redesign
"""

import json
from pathlib import Path

import pytest

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.bootstrap.services import prepare_for_runtime_write
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.types import HarnessId
from meridian.lib.ops.spawn.models import SpawnCreateInput
from meridian.lib.ops.worktree_ops import resolve_worktree_path
from meridian.lib.state import work_store
from meridian.lib.state.paths import resolve_project_paths
from meridian.lib.state.work_store import WorktreeMetadata
from meridian.lib.telemetry import init_telemetry
from tests.support.fakes import RecordingTelemetrySink, wait_for_telemetry
from tests.support.launch import stub_bundle_request_and_resolve


def _noop_setup_telemetry(**_kwargs: object) -> None:
    pass


def test_spawn_create_dry_run_resolves_project_root_from_nested_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    nested = project_root / "src" / "feature"
    (project_root / ".mars" / "skills").mkdir(parents=True)
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    nested.mkdir(parents=True)
    reference_file = project_root / "guide.md"
    reference_file.write_text("# Guide\n", encoding="utf-8")
    monkeypatch.chdir(nested)

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="gpt-5.4-mini",
            files=("guide.md",),
            dry_run=True,
        )
    )

    assert result.status == "dry-run"
    assert result.project_root == project_root.resolve().as_posix()
    assert result.project_root_source == "mars"
    assert result.runtime_root is None
    assert result.runtime_root_source == "unresolved"
    resolved_reference = reference_file.resolve()
    assert len(result.reference_files) == 1
    assert Path(result.reference_files[0]).resolve() == resolved_reference
    composed_prompt = result.composed_prompt or ""
    assert (
        str(resolved_reference) in composed_prompt
        or resolved_reference.as_posix() in composed_prompt
    )


def test_spawn_create_dry_run_emits_usage_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(project_root)
    monkeypatch.setattr(spawn_api, "setup_telemetry", _noop_setup_telemetry)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.3-codex",
        harness=HarnessId.CODEX,
    )
    sink = RecordingTelemetrySink()
    init_telemetry(sink=sink)

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="gpt-5.3-codex",
            harness="codex",
            project_root=project_root.as_posix(),
            dry_run=True,
        )
    )

    assert result.status == "dry-run"
    wait_for_telemetry(
        lambda: {"usage.model.selected", "usage.spawn.launched"}.issubset(
            {event.event for event in sink.events}
        )
    )
    usage_events = {event.event: event for event in sink.events if event.domain == "usage"}
    assert usage_events["usage.model.selected"].data == {
        "model_family": "gpt-5.3",
        "harness": "codex",
    }
    assert "gpt-5.3-codex" not in json.dumps(usage_events["usage.model.selected"].to_dict())
    assert usage_events["usage.spawn.launched"].data == {"harness": "codex"}


def test_spawn_create_with_prepared_skips_self_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    prepared = prepare_for_runtime_write(project_root)

    def _forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("self-bootstrap helper should not be called")

    monkeypatch.setattr(spawn_api, "setup_telemetry", _forbidden)
    monkeypatch.setattr(spawn_api, "load_config", _forbidden)
    monkeypatch.setattr(spawn_api, "resolve_runtime_root_and_config", _forbidden)
    monkeypatch.setattr(spawn_api, "resolve_runtime_root", _forbidden)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.3-codex",
        harness=HarnessId.CODEX,
    )

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="gpt-5.3-codex",
            harness="codex",
            project_root=project_root.as_posix(),
            dry_run=True,
        ),
        prepared=prepared,
    )

    assert result.status == "dry-run"
    assert result.harness_id == "codex"


def test_spawn_create_dry_run_surfaces_goal_and_contract_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    monkeypatch.setattr(
        spawn_api,
        "build_create_payload",
        lambda payload,
        runtime=None,
        preflight_warning=None,
        ctx=None,
        forced_task_cwd_resolution=None: type(
            "Prepared",
            (),
            {
                "harness": payload.harness or "codex",
                "model": payload.model,
                "warning": preflight_warning,
                "agent": payload.agent,
                "agent_metadata": {},
                "skills": payload.skills,
                "skill_paths": (),
                "reference_files": (),
                "template_vars": {},
                "context_from": (),
                "prompt": payload.prompt,
                "goal": payload.goal,
                "model_selection_requested_token": None,
                "model_selection_canonical_id": None,
                "model_selection_harness_provenance": None,
                "terminal_surface_mode": None,
                "cli_command": ("codex",),
            },
        )(),
    )

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            goal="ship phase 3",
            project_root=project_root.as_posix(),
            dry_run=True,
        )
    )

    assert result.status == "dry-run"
    assert result.goal == "ship phase 3"
    goal_contract_preview = result.goal_contract_preview
    assert goal_contract_preview is not None
    assert "# Spawn Goal" in goal_contract_preview
    assert "ship phase 3" in goal_contract_preview


def test_spawn_create_dry_run_with_work_is_non_mutating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(project_root)
    project_state_dir = resolve_project_paths(project_root).root_dir

    assert work_store.get_work_item(project_state_dir, "new-work-item") is None

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="gpt-5.4-mini",
            work="new-work-item",
            project_root=project_root.as_posix(),
            dry_run=True,
        )
    )

    assert result.status == "dry-run"
    assert work_store.get_work_item(project_state_dir, "new-work-item") is None
    assert result.warning is not None
    assert "would be created on launch" in result.warning


def test_spawn_create_dry_run_worktree_forces_canonical_task_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(project_root)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4-mini",
        harness=HarnessId.CODEX,
    )
    project_state_dir = resolve_project_paths(project_root).root_dir
    work = work_store.create_work_item(project_state_dir, "ensure-worktree", "", None)
    canonical_path = resolve_worktree_path(project_root, work.name)
    work_store.update_work_item_worktree(
        project_state_dir,
        work.name,
        path=canonical_path.as_posix(),
        branch=f"feature/{work.name}",
        repo_path=project_root.as_posix(),
        name=work.name,
        pending=True,
        managed=True,
    )

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="gpt-5.4-mini",
            work=work.name,
            worktree=True,
            project_root=project_root.as_posix(),
            dry_run=True,
        )
    )

    assert result.status == "dry-run"
    assert result.task_cwd_source == "forced-worktree"
    assert result.task_cwd == canonical_path.as_posix()
    assert result.reference_anchor == canonical_path.as_posix()
    assert result.task_cwd_work_item == work.name
    assert "Pending marker present" in (result.warning or "")


def test_spawn_create_dry_run_worktree_uses_ambient_work_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(project_root)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4-mini",
        harness=HarnessId.CODEX,
    )
    project_state_dir = resolve_project_paths(project_root).root_dir
    work = work_store.create_work_item(project_state_dir, "ambient-worktree", "", None)
    canonical_path = resolve_worktree_path(project_root, work.name)

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="gpt-5.4-mini",
            worktree=True,
            project_root=project_root.as_posix(),
            dry_run=True,
        ),
        ctx=RuntimeContext(work_id=work.name),
    )

    assert result.status == "dry-run"
    assert result.task_cwd_source == "forced-worktree"
    assert result.task_cwd == canonical_path.as_posix()
    assert result.reference_anchor == canonical_path.as_posix()
    assert result.task_cwd_work_item == work.name
    updated = work_store.get_work_item(project_state_dir, work.name)
    assert updated is not None
    assert updated.worktree == WorktreeMetadata()
