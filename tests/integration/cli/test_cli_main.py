# qa-validated: pi-rpc-quiescence
import importlib
import os
import shlex
import threading
from pathlib import Path
from typing import Any

import pytest

from meridian.cli.startup.catalog import StartupClass
from meridian.lib.core.types import HarnessId
from meridian.lib.ops import diag as ops_diag
from meridian.lib.state.user_paths import get_project_home
from tests.support.fixtures import write_minimal_mars_config
from tests.support.launch import stub_bundle_request_and_resolve
from tests.support.pi_extensions import configure_pi_extension_projection

cli_main = importlib.import_module("meridian.cli.main")
config_cmd = importlib.import_module("meridian.cli.config_cmd")
hooks_cli = importlib.import_module("meridian.cli.hooks_commands")
ops_hooks = importlib.import_module("meridian.lib.ops.hooks")
primary_launch = importlib.import_module("meridian.cli.primary_launch")
session_cmd = importlib.import_module("meridian.cli.session_cmd")
init_ops = importlib.import_module("meridian.lib.ops.init_ops")
bootstrap_services = importlib.import_module("meridian.lib.bootstrap.services")
cli_utils = importlib.import_module("meridian.cli.utils")


def test_main_restores_directory_env_after_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", "/prior/project")
    monkeypatch.setenv("MERIDIAN_DIRECTORY_EXPLICIT", "1")
    monkeypatch.setattr(cli_main, "_install_cli_telemetry", lambda **_kwargs: None)
    monkeypatch.setattr(cli_main, "_maybe_schedule_background_repairs", lambda **_kwargs: None)
    monkeypatch.setattr(cli_main, "_emit_usage_command_invoked", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "maybe_bootstrap_runtime_state",
        lambda *_args, **_kwargs: tmp_path / "resolved-project",
    )
    monkeypatch.setattr(
        hooks_cli,
        "hooks_check_sync",
        lambda _payload: ops_hooks.HookCheckOutput(ok=True, checks=()),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["hooks", "check"])

    assert exc_info.value.code == 0
    assert os.environ.get("MERIDIAN_PROJECT_DIR") == "/prior/project"
    assert os.environ.get("MERIDIAN_DIRECTORY_EXPLICIT") == "1"


def test_background_repairs_pin_runtime_root_before_env_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "repo"
    state_dir = project / ".meridian"
    state_dir.mkdir(parents=True)
    (state_dir / "id").write_text("proj-repair", encoding="utf-8")
    stale_runtime = tmp_path / "stale-runtime"
    stale_runtime.mkdir()
    expected_runtime = get_project_home("proj-repair")

    monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", stale_runtime.as_posix())
    monkeypatch.setenv("MERIDIAN_DIRECTORY_EXPLICIT", "1")

    captured: dict[str, Path | None] = {}

    def _capture_locks(
        project_root: Path,
        *,
        runtime_root: Path | None = None,
    ) -> int:
        captured["locks"] = runtime_root
        monkeypatch.delenv("MERIDIAN_DIRECTORY_EXPLICIT", raising=False)
        monkeypatch.delenv("MERIDIAN_RUNTIME_DIR", raising=False)
        assert runtime_root == expected_runtime
        return 0

    def _capture_orphans(
        project_root: Path,
        *,
        runtime_root: Path | None = None,
    ) -> tuple[int, tuple[str, ...]]:
        captured["orphans"] = runtime_root
        assert runtime_root == expected_runtime
        return 0, ()

    class _ImmediateThread:
        def __init__(
            self,
            *,
            target,
            name: str | None = None,
            daemon: bool | None = None,
        ) -> None:
            self._target = target

        def start(self) -> None:
            self._target()

    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(ops_diag, "_repair_stale_session_locks", _capture_locks)
    monkeypatch.setattr(ops_diag, "_repair_orphan_runs", _capture_orphans)

    cli_main._maybe_schedule_background_repairs(
        startup_class=StartupClass.PRIMARY_LAUNCH,
        project_root=project,
        bootstrap_skipped=False,
    )

    assert captured["locks"] == expected_runtime
    assert captured["orphans"] == expected_runtime


def test_main_sets_directory_env_before_telemetry_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
    project = tmp_path / "explicit-project"
    project.mkdir()
    (project / ".meridian").mkdir()
    stale_runtime = tmp_path / "stale-runtime"
    stale_runtime.mkdir()
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", stale_runtime.as_posix())
    monkeypatch.delenv("MERIDIAN_DIRECTORY_EXPLICIT", raising=False)
    telemetry_env: dict[str, str | None] = {}

    def _capture_telemetry(**_kwargs: object) -> None:
        telemetry_env["MERIDIAN_PROJECT_DIR"] = os.environ.get("MERIDIAN_PROJECT_DIR")
        telemetry_env["MERIDIAN_DIRECTORY_EXPLICIT"] = os.environ.get(
            "MERIDIAN_DIRECTORY_EXPLICIT"
        )

    monkeypatch.setattr(cli_main, "_install_cli_telemetry", _capture_telemetry)
    monkeypatch.setattr(cli_main, "_maybe_schedule_background_repairs", lambda **_kwargs: None)
    monkeypatch.setattr(cli_main, "_emit_usage_command_invoked", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "maybe_bootstrap_runtime_state",
        lambda *_args, **_kwargs: project.resolve(),
    )
    monkeypatch.setattr(
        hooks_cli,
        "hooks_check_sync",
        lambda _payload: ops_hooks.HookCheckOutput(ok=True, checks=()),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["-C", project.as_posix(), "hooks", "check"])

    assert exc_info.value.code == 0
    assert telemetry_env["MERIDIAN_PROJECT_DIR"] == project.resolve().as_posix()
    assert telemetry_env["MERIDIAN_DIRECTORY_EXPLICIT"] == "1"


def test_directory_flag_supplies_project_root_to_config_ops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "target-project"
    project.mkdir()
    parent_project = tmp_path / "parent-project"
    parent_project.mkdir()
    stale_runtime = tmp_path / "stale-runtime"
    stale_runtime.mkdir()
    captured: dict[str, str | None] = {}

    monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", parent_project.as_posix())
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", stale_runtime.as_posix())
    monkeypatch.setattr(cli_main, "_install_cli_telemetry", lambda **_kwargs: None)
    monkeypatch.setattr(cli_main, "_maybe_schedule_background_repairs", lambda **_kwargs: None)
    monkeypatch.setattr(cli_main, "_emit_usage_command_invoked", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "maybe_bootstrap_runtime_state",
        lambda *_args, **_kwargs: project.resolve(),
    )

    def _capture_config_get(payload: Any) -> object:
        captured["project_root"] = payload.project_root
        raise SystemExit(0)

    monkeypatch.setattr(config_cmd, "config_get_sync", _capture_config_get)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["-C", project.as_posix(), "config", "get", "model"])

    assert exc_info.value.code == 0
    assert captured["project_root"] == project.resolve().as_posix()


def test_directory_flag_supplies_project_root_to_session_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "target-project"
    project.mkdir()
    parent_project = tmp_path / "parent-project"
    parent_project.mkdir()
    stale_runtime = tmp_path / "stale-runtime"
    stale_runtime.mkdir()
    captured: dict[str, str | None] = {}

    monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", parent_project.as_posix())
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", stale_runtime.as_posix())
    monkeypatch.setattr(cli_main, "_install_cli_telemetry", lambda **_kwargs: None)
    monkeypatch.setattr(cli_main, "_maybe_schedule_background_repairs", lambda **_kwargs: None)
    monkeypatch.setattr(cli_main, "_emit_usage_command_invoked", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "maybe_bootstrap_runtime_state",
        lambda *_args, **_kwargs: project.resolve(),
    )

    def _capture_session_log(payload: Any) -> object:
        captured["project_root"] = payload.project_root
        raise SystemExit(0)

    monkeypatch.setattr(session_cmd, "session_log_sync", _capture_session_log)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["-C", project.as_posix(), "session", "log", "c8"])

    assert exc_info.value.code == 0
    assert captured["project_root"] == project.resolve().as_posix()


def test_main_skips_fork_normalization_for_mars_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_run_mars_passthrough(
        args: list[str],
        *,
        output_format: str | None = None,
    ) -> None:
        captured["args"] = tuple(args)
        captured["output_format"] = output_format
        raise SystemExit(0)

    monkeypatch.setattr(cli_main, "_run_mars_passthrough", _fake_run_mars_passthrough)
    monkeypatch.setattr(cli_main, "_emit_usage_command_invoked", lambda *_a, **_k: None)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["mars", "models", "list", "--fork"])

    assert exc_info.value.code == 0
    assert captured["args"] == ("models", "list", "--fork")
    assert captured["output_format"] == "text"


def test_main_pi_primary_launch_dry_run_is_supported(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    write_minimal_mars_config(tmp_path)
    configure_pi_extension_projection(monkeypatch, tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4-mini",
        harness=HarnessId.PI,
    )
    monkeypatch.setattr(
        cli_main,
        "maybe_bootstrap_runtime_state",
        lambda *_args, **_kwargs: tmp_path,
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--harness", "pi", "--model", "gpt-5.4-mini", "--dry-run"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "pi" in captured.out
    command_start = captured.out.find("pi ")
    assert command_start >= 0
    dry_run_argv = shlex.split(captured.out[command_start:])
    assert "--mode" not in dry_run_argv
    assert "--mode rpc" not in captured.out


def test_bootstrap_with_setup_runs_init_flow_and_passes_project_root_to_primary_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_init: dict[str, object] = {}
    captured_launch: dict[str, object] = {}
    repo_root = tmp_path / "repo-root"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    nested_cwd = repo_root / "nested" / "project"
    nested_cwd.mkdir(parents=True)
    monkeypatch.chdir(nested_cwd)
    monkeypatch.delenv("MERIDIAN_PROJECT_DIR", raising=False)
    monkeypatch.setattr(
        cli_utils,
        "require_established_project_root",
        lambda: pytest.fail("unexpected startup root resolution"),
    )
    monkeypatch.setattr(
        bootstrap_services,
        "prepare_for_runtime_write",
        lambda _project_root: pytest.fail("unexpected startup runtime bootstrap"),
    )

    def _fake_run_init_flow(
        *,
        project_root: Path,
        add_sources: list[str],
        link_targets: list[str] | None = None,
        output_format: str = "text",
    ) -> object:
        captured_init.update(
            {
                "project_root": project_root.as_posix(),
                "add_sources": add_sources,
                "link_targets": link_targets,
                "output_format": output_format,
            }
        )
        return object()

    def _fake_primary_launch(**kwargs: object) -> object:
        captured_launch.update(kwargs)
        return primary_launch.PrimaryLaunchOutput(message="ok", exit_code=0)

    monkeypatch.setattr(init_ops, "run_init_flow", _fake_run_init_flow)
    monkeypatch.setattr(primary_launch, "run_primary_launch", _fake_primary_launch)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(
            [
                "bootstrap",
                "--add",
                "haowjy/meridian-dev-workflow",
                "--add",
                "haowjy/meridian-base",
                "--link",
                ".claude",
                "--link",
                ".cursor",
            ]
        )

    assert exc_info.value.code == 0
    expected_root = nested_cwd.resolve().as_posix()
    assert captured_init == {
        "project_root": expected_root,
        "add_sources": ["haowjy/meridian-dev-workflow", "haowjy/meridian-base"],
        "link_targets": [".claude", ".cursor"],
        "output_format": "text",
    }
    assert captured_launch["project_root"] == nested_cwd.resolve()
    assert captured_launch["include_bootstrap_documents"] is True


def test_bootstrap_setup_flags_reject_dry_run_before_setup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo-root"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    nested_cwd = repo_root / "nested" / "project"
    nested_cwd.mkdir(parents=True)
    monkeypatch.chdir(nested_cwd)
    monkeypatch.delenv("MERIDIAN_PROJECT_DIR", raising=False)
    monkeypatch.setattr(
        cli_utils,
        "require_established_project_root",
        lambda: pytest.fail("unexpected startup root resolution"),
    )
    monkeypatch.setattr(
        bootstrap_services,
        "prepare_for_runtime_write",
        lambda _project_root: pytest.fail("unexpected startup runtime bootstrap"),
    )
    monkeypatch.setattr(init_ops, "run_init_flow", lambda **_kwargs: pytest.fail("unexpected init"))
    monkeypatch.setattr(
        primary_launch,
        "run_primary_launch",
        lambda **_kwargs: pytest.fail("unexpected primary launch"),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["bootstrap", "--add", "haowjy/meridian-dev-workflow", "--dry-run"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: Cannot combine setup flags (--add/--link) with --dry-run.\n"
