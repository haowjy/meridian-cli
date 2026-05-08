from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from meridian.lib.bootstrap.services import prepare_for_runtime_write
from meridian.lib.ops.spawn import api as spawn_api
from meridian.lib.ops.spawn.models import (
    SpawnActionOutput,
    SpawnCancelAllInput,
    SpawnContinueInput,
    SpawnForkInput,
)


def test_resolve_spawn_operation_services_prefers_prepared_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    prepared = prepare_for_runtime_write(project_root)
    sentinel_service = object()

    monkeypatch.setattr(
        spawn_api,
        "build_spawn_application_service",
        lambda _prepared: sentinel_service,
    )

    services = spawn_api.resolve_spawn_operation_services(
        project_root=None,
        prepared=prepared,
    )

    assert services.project_root == project_root
    assert services.runtime_root == prepared.runtime_root
    assert services.spawn_service is sentinel_service


def test_resolve_spawn_operation_services_builds_from_roots_without_prepared(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    runtime_root = tmp_path / "runtime"
    captured: dict[str, Path] = {}
    sentinel_service = object()

    monkeypatch.setattr(
        spawn_api,
        "resolve_runtime_root_and_config",
        lambda _project_root: (project_root, object()),
    )
    monkeypatch.setattr(spawn_api, "resolve_runtime_root", lambda _project_root: runtime_root)

    def _build_from_roots(project: Path, runtime: Path) -> object:
        captured["project_root"] = project
        captured["runtime_root"] = runtime
        return sentinel_service

    monkeypatch.setattr(spawn_api, "build_spawn_application_service_from_roots", _build_from_roots)

    services = spawn_api.resolve_spawn_operation_services(project_root="ignored", prepared=None)

    assert services.project_root == project_root
    assert services.runtime_root == runtime_root
    assert services.spawn_service is sentinel_service
    assert captured == {
        "project_root": project_root,
        "runtime_root": runtime_root,
    }


def test_spawn_cancel_all_sync_resolves_roots_via_spawn_operation_services(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    runtime_root = tmp_path / "runtime"
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        spawn_api,
        "resolve_spawn_operation_services",
        lambda *, project_root, prepared=None: SimpleNamespace(
            project_root=Path(project_root) if project_root else None,
            runtime_root=runtime_root,
            spawn_service=object(),
        ),
    )
    monkeypatch.setattr(spawn_api.spawn_store, "list_spawns", lambda _runtime_root: [])

    def _fake_reconcile(_project_root, _runtime_root, rows):
        captured["project_root"] = _project_root
        captured["runtime_root"] = _runtime_root
        return rows

    monkeypatch.setattr("meridian.lib.state.reaper.reconcile_spawns", _fake_reconcile)

    output = spawn_api.spawn_cancel_all_sync(
        SpawnCancelAllInput(project_root=project_root.as_posix()),
    )

    assert output.total_running == 0
    assert output.cancelled_count == 0
    assert output.failed_count == 0
    assert captured == {
        "project_root": project_root,
        "runtime_root": runtime_root,
    }


def test_spawn_continue_sync_resolves_roots_via_read_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    runtime_root = tmp_path / "runtime"
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        spawn_api,
        "_resolve_spawn_read_authority",
        lambda *, project_root, prepared=None: (
            Path(project_root) if project_root else Path("/resolved/project"),
            runtime_root,
        ),
    )

    source_spawn = SimpleNamespace(prompt="seed prompt", model="gpt-5.4")
    resolved_reference = SimpleNamespace(
        missing_harness_session_id=False,
        harness="codex",
        tracked=True,
        source_chat_id="c10",
        source_execution_cwd="/tmp/source",
        source_claude_config_dir="/tmp/claude",
        harness_session_id="session-10",
    )

    def _fake_source_for_follow_up(spawn_id, resolved_project_root, *, runtime_root=None):
        captured["source_spawn_id"] = spawn_id
        captured["project_root"] = resolved_project_root
        captured["runtime_root"] = runtime_root
        return "p10", source_spawn, resolved_reference

    monkeypatch.setattr(spawn_api, "_source_spawn_for_follow_up", _fake_source_for_follow_up)
    monkeypatch.setattr(
        spawn_api,
        "spawn_create_sync",
        lambda *_args, **_kwargs: SpawnActionOutput(command="spawn.create", status="dry-run"),
    )

    result = spawn_api.spawn_continue_sync(
        SpawnContinueInput(
            spawn_id="p10",
            prompt="follow-up",
            project_root=project_root.as_posix(),
        )
    )

    assert result.command == "spawn.continue"
    assert result.status == "dry-run"
    assert captured == {
        "source_spawn_id": "p10",
        "project_root": project_root,
        "runtime_root": runtime_root,
    }


def test_spawn_fork_sync_resolves_roots_via_read_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    runtime_root = tmp_path / "runtime"
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        spawn_api,
        "_resolve_spawn_read_authority",
        lambda *, project_root, prepared=None: (
            Path(project_root) if project_root else Path("/resolved/project"),
            runtime_root,
        ),
    )

    def _fake_resolve_session_reference(
        resolved_project_root,
        source_ref,
        *,
        runtime_root=None,
    ):
        captured["project_root"] = resolved_project_root
        captured["source_ref"] = source_ref
        captured["runtime_root"] = runtime_root
        return SimpleNamespace(
            missing_harness_session_id=False,
            source_skills=("skill-1",),
            source_model="gpt-5.4",
            source_agent="coder",
            source_work_id="w-source",
            harness="codex",
            harness_session_id="session-10",
            tracked=True,
            source_chat_id="c10",
            source_execution_cwd="/tmp/source",
            source_claude_config_dir="/tmp/claude",
        )

    monkeypatch.setattr(spawn_api, "resolve_session_reference", _fake_resolve_session_reference)
    monkeypatch.setattr(
        spawn_api,
        "spawn_create_sync",
        lambda *_args, **_kwargs: SpawnActionOutput(command="spawn.create", status="dry-run"),
    )

    result = spawn_api.spawn_fork_sync(
        SpawnForkInput(
            source_ref="c10",
            prompt="fork",
            project_root=project_root.as_posix(),
            inherit_source_skills=True,
        )
    )

    assert result.command == "spawn.create"
    assert result.status == "dry-run"
    assert captured == {
        "project_root": project_root,
        "source_ref": "c10",
        "runtime_root": runtime_root,
    }
