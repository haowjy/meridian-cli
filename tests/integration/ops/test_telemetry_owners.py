from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import meridian.cli.chat_cmd as chat_cmd
import meridian.lib.ops.spawn.api as spawn_api
import meridian.lib.ops.spawn.execute as spawn_execute
import meridian.lib.telemetry.bootstrap as telemetry_bootstrap
from meridian.lib.ops.spawn.models import SpawnCreateInput


class _StopAfterTelemetrySetup(Exception):
    pass


def _stub_spawn_create_dry_run(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spawn_api, "_resolve_project_root_input", lambda _path: project_root)
    monkeypatch.setattr(spawn_api, "load_config", lambda _root: SimpleNamespace(max_depth=1))
    monkeypatch.setattr(spawn_api, "validate_create_input", lambda payload: (payload, None))
    monkeypatch.setattr(spawn_api, "_emit_usage_spawn_launched", lambda harness: None)
    monkeypatch.setattr(
        spawn_api,
        "build_create_payload",
        lambda payload, runtime, preflight_warning, ctx: SimpleNamespace(
            harness="codex",
            model="gpt-5.3-codex",
            warning=None,
            agent="",
            agent_metadata={},
            skills=(),
            skill_paths=(),
            reference_files=(),
            template_vars={},
            context_from=(),
            prompt="",
            model_selection_requested_token=None,
            model_selection_canonical_id=None,
            model_selection_harness_provenance=None,
            cli_command=(),
        ),
    )


def test_spawn_create_sync_uses_cli_owner_without_spawn_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)
    captured: dict[str, object] = {}
    _stub_spawn_create_dry_run(project_root, monkeypatch)

    def _capture_setup_telemetry(*, runtime_root=None, logical_owner=None, **_kwargs):
        captured["runtime_root"] = runtime_root
        captured["logical_owner"] = logical_owner

    monkeypatch.setattr(spawn_api, "setup_telemetry", _capture_setup_telemetry)
    monkeypatch.setattr(spawn_api, "register_spawn_telemetry_observer", lambda: None)

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
    assert captured == {"runtime_root": None, "logical_owner": "cli"}


def test_spawn_create_sync_uses_inherited_spawn_owner_from_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p123")
    captured: dict[str, object] = {}
    _stub_spawn_create_dry_run(project_root, monkeypatch)

    def _capture_setup_telemetry(*, runtime_root=None, logical_owner=None, **_kwargs):
        captured["runtime_root"] = runtime_root
        captured["logical_owner"] = logical_owner

    monkeypatch.setattr(spawn_api, "setup_telemetry", _capture_setup_telemetry)
    monkeypatch.setattr(spawn_api, "register_spawn_telemetry_observer", lambda: None)

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
    assert captured == {"runtime_root": None, "logical_owner": "p123"}


def test_background_worker_main_uses_spawn_id_as_logical_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    project_root = tmp_path / "repo"
    project_root.mkdir()
    captured: dict[str, object] = {}

    def _capture_install(plan):
        captured["runtime_root"] = plan.runtime_root
        captured["logical_owner"] = plan.logical_owner
        return SimpleNamespace(mode=plan.mode)

    monkeypatch.setattr(telemetry_bootstrap, "install", _capture_install)
    monkeypatch.setattr(
        spawn_execute,
        "prepare_for_runtime_write",
        lambda _root: SimpleNamespace(project_root=project_root, runtime_root=runtime_root),
    )
    monkeypatch.setattr(spawn_execute, "register_spawn_telemetry_observer", lambda: None)
    monkeypatch.setattr(
        spawn_execute,
        "resolve_project_config_paths",
        lambda *, project_root: SimpleNamespace(project_root=project_root),
    )
    monkeypatch.setattr(spawn_execute, "resolve_runtime_root", lambda _root: runtime_root)
    monkeypatch.setattr(
        spawn_execute,
        "resolve_spawn_log_dir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(_StopAfterTelemetrySetup()),
    )
    monkeypatch.setattr(spawn_execute, "create_lifecycle_service", lambda *_a, **_kw: None)
    monkeypatch.setattr(spawn_execute, "_complete_spawn_sync", lambda **_kw: False)

    result = spawn_execute._background_worker_main(
        ["--spawn-id", "p77", "--project-root", project_root.as_posix()]
    )

    assert result == 1  # backstop catches the injected exception
    assert captured == {"runtime_root": runtime_root, "logical_owner": "p77"}


def test_run_chat_server_uses_prepared_runtime_write_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    project_root = tmp_path / "repo"
    project_root.mkdir()
    captured: dict[str, object] = {}

    monkeypatch.setattr(chat_cmd, "resolve_project_root", lambda: project_root)
    monkeypatch.setattr(
        chat_cmd,
        "prepare_for_runtime_write",
        lambda root: SimpleNamespace(project_root=root, runtime_root=runtime_root),
    )

    def _capture_chat_runtime(**kwargs):
        captured["chat_project_root"] = kwargs["project_root"]
        captured["chat_runtime_root"] = kwargs["runtime_root"]
        return object()

    monkeypatch.setattr(chat_cmd, "ChatRuntime", _capture_chat_runtime)
    monkeypatch.setattr(
        "meridian.lib.chat.server.configure",
        lambda **_kwargs: (_ for _ in ()).throw(_StopAfterTelemetrySetup()),
    )
    monkeypatch.setattr(
        chat_cmd,
        "get_user_home",
        lambda: runtime_root,
    )

    with pytest.raises(_StopAfterTelemetrySetup):
        chat_cmd.run_chat_server(harness="codex", headless=True)

    assert captured == {
        "chat_project_root": project_root,
        "chat_runtime_root": runtime_root,
    }
