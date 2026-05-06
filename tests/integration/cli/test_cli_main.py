import importlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from meridian.cli.app_tree import AGENT_ROOT_HELP
from meridian.cli.startup.help import AGENT_ROOT_HELP as STARTUP_AGENT_ROOT_HELP
from meridian.cli.startup.policy import StartupClass, StateRequirement
from meridian.lib.state import paths as state_paths
from meridian.lib.telemetry import emit_telemetry
from meridian.lib.telemetry.router import get_global_router

bootstrap_cmd = importlib.import_module("meridian.cli.bootstrap_cmd")
cli_main = importlib.import_module("meridian.cli.main")
mars_passthrough = importlib.import_module("meridian.cli.mars_passthrough")
primary_launch = importlib.import_module("meridian.cli.primary_launch")
config_ops = importlib.import_module("meridian.lib.ops.config")


def test_main_rejects_unknown_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["exec"])

    assert exc_info.value.code == 1
    assert "Unknown command: exec" in capsys.readouterr().err


def test_main_skips_bootstrap_for_subcommand_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_bootstrap(
        argv: list[str],
        *,
        agent_mode: bool,
        state_requirement: StateRequirement | None = None,
    ) -> None:
        raise AssertionError(
            f"bootstrap should be skipped for help, got {argv=} {agent_mode=} "
            f"{state_requirement=}"
        )

    monkeypatch.setattr(cli_main, "maybe_bootstrap_runtime_state", _fake_bootstrap)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["config", "show", "--help"])

    assert exc_info.value.code == 0


def test_main_emits_normalized_usage_command(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, object]] = []

    def _fake_emit_telemetry(
        domain: str,
        event: str,
        *,
        scope: str,
        data: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        captured.append({"domain": domain, "event": event, "scope": scope, "data": data})

    monkeypatch.setattr(cli_main, "emit_telemetry", _fake_emit_telemetry)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "wait", "p1", "--timeout", "0"])

    assert exc_info.value.code == 1
    assert captured[0] == {
        "domain": "usage",
        "event": "usage.command.invoked",
        "scope": "cli.dispatch",
        "data": {"command": "spawn.wait"},
    }


def test_main_harness_shortcut_routes_into_primary_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    captured: dict[str, object] = {}

    def _fake_primary_launch(**kwargs: object) -> object:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(primary_launch, "run_primary_launch", _fake_primary_launch)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["codex", "--dry-run"])

    assert exc_info.value.code == 0
    assert captured["harness"] == "codex"
    assert captured["dry_run"] is True


@pytest.mark.parametrize(
    ("argv", "agent_depth"),
    [
        (["models", "list"], None),
        (["--format", "json", "models", "list"], None),
        (["models", "list"], "1"),
    ],
    ids=["human-text", "human-json", "agent-mode"],
)
def test_main_models_list_redirect_stays_in_startup_layer(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    agent_depth: str | None,
) -> None:
    original_models_cmd = sys.modules.pop("meridian.cli.models_cmd", None)
    try:
        monkeypatch.setattr(
            cli_main,
            "maybe_bootstrap_runtime_state",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("models list redirect should not bootstrap runtime state")
            ),
        )
        if agent_depth is None:
            monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
        else:
            monkeypatch.setenv("MERIDIAN_DEPTH", agent_depth)

        with pytest.raises(SystemExit) as exc_info:
            cli_main.main(argv)

        assert exc_info.value.code == 1
        assert "meridian models list" in capsys.readouterr().err
        assert "meridian.cli.models_cmd" not in sys.modules
    finally:
        if original_models_cmd is not None:
            sys.modules["meridian.cli.models_cmd"] = original_models_cmd


def test_primary_launch_background_repairs_stay_within_current_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    user_home = tmp_path / "user-home"
    repair_calls: list[tuple[str, Path]] = []
    started_threads: list[threading.Thread] = []

    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    monkeypatch.setattr(cli_main, "is_root_side_effect_process", lambda: True)
    monkeypatch.setattr(
        "meridian.lib.ops.diag._repair_stale_session_locks",
        lambda root: repair_calls.append(("stale_session_locks", root)) or 0,
    )
    monkeypatch.setattr(
        "meridian.lib.ops.diag._repair_orphan_runs",
        lambda root: repair_calls.append(("orphan_runs", root)) or 0,
    )
    monkeypatch.setattr(
        "meridian.lib.ops.diag.scan_orphan_project_dirs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("PRIMARY_LAUNCH repairs should stay within the current project")
        ),
    )

    monkeypatch.setattr(threading.Thread, "start", lambda self: started_threads.append(self))

    cli_main._maybe_schedule_background_repairs(
        startup_class=StartupClass.PRIMARY_LAUNCH,
        project_root=project_root,
        bootstrap_skipped=False,
    )

    assert len(started_threads) == 1
    assert started_threads[0].daemon is True
    assert "repair" in started_threads[0].name.lower()
    started_threads[0].run()
    assert repair_calls == [
        ("stale_session_locks", project_root),
        ("orphan_runs", project_root),
    ]
    assert not (user_home / "doctor-cache.json").exists()


def test_install_cli_telemetry_writes_usage_events_to_local_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)

    monkeypatch.setattr(
        state_paths,
        "resolve_project_runtime_root_for_write",
        lambda _project_root: runtime_root,
    )

    try:
        cli_main._install_cli_telemetry(
            telemetry_mode=cli_main.StartupTelemetryMode.SEGMENT,
            startup_class=StartupClass.WRITE_RUNTIME,
            project_root=project_root,
        )
        emit_telemetry(
            "usage",
            "usage.spawn.launched",
            scope="cli.dispatch",
            data={"harness": "codex"},
        )

        segment = runtime_root / "telemetry" / f"cli.{os.getpid()}-0001.jsonl"
        for _ in range(100):
            if segment.exists() and len(segment.read_text(encoding="utf-8").splitlines()) >= 1:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("telemetry was not written to local jsonl")

        events = [
            json.loads(line)["event"]
            for line in segment.read_text(encoding="utf-8").splitlines()
        ]
        assert events == ["usage.spawn.launched"]
    finally:
        get_global_router().close()


def test_install_cli_telemetry_uses_inherited_spawn_owner_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p123")
    monkeypatch.setattr(
        state_paths,
        "resolve_project_runtime_root_for_write",
        lambda _project_root: runtime_root,
    )

    try:
        cli_main._install_cli_telemetry(
            telemetry_mode=cli_main.StartupTelemetryMode.SEGMENT,
            startup_class=StartupClass.WRITE_RUNTIME,
            project_root=project_root,
        )
        emit_telemetry("usage", "usage.spawn.launched", scope="cli.dispatch")

        segment = runtime_root / "telemetry" / f"p123.{os.getpid()}-0001.jsonl"
        for _ in range(100):
            if segment.exists() and len(segment.read_text(encoding="utf-8").splitlines()) >= 1:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("buffered telemetry was not replayed with inherited owner")

        events = [
            json.loads(line)["event"]
            for line in segment.read_text(encoding="utf-8").splitlines()
        ]
        assert events == ["usage.spawn.launched"]
    finally:
        get_global_router().close()


def test_main_does_not_create_segment_telemetry_when_project_root_never_resolves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    user_home = tmp_path / "user-home"
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
    monkeypatch.setattr(cli_main, "maybe_bootstrap_runtime_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "meridian.lib.config.project_root.resolve_project_root",
        lambda: (_ for _ in ()).throw(ValueError("no project")),
    )

    try:
        with pytest.raises(SystemExit) as exc_info:
            cli_main.main(["doctor", "--help"])

        assert exc_info.value.code == 0
        assert (user_home / "telemetry").exists() is False
        assert list(user_home.rglob("*.jsonl")) == []
        assert "doctor" in capsys.readouterr().out
    finally:
        get_global_router().close()


@pytest.mark.parametrize(
    "argv",
    [
        ["chat"],
        ["chat", "ls"],
        ["chat", "show", "c1"],
        ["chat", "log", "c1"],
        ["chat", "close", "c1"],
    ],
)
def test_main_rejects_nested_chat_commands_before_chat_runtime_preparation(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(
        "meridian.cli.chat_cmd.prepare_for_runtime_read",
        lambda *_args, **_kwargs: calls.append("read"),
    )
    monkeypatch.setattr(
        "meridian.cli.chat_cmd.prepare_for_runtime_write",
        lambda *_args, **_kwargs: calls.append("write"),
    )
    monkeypatch.setattr(
        "meridian.cli.chat_cmd.get_user_home",
        lambda: (_ for _ in ()).throw(AssertionError("nested chat should fail before user-home")),
    )
    monkeypatch.setattr(
        "meridian.lib.bootstrap.services.prepare_for_runtime_read",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("nested chat should not bootstrap runtime state")
        ),
    )
    monkeypatch.setattr(
        "meridian.lib.bootstrap.services.prepare_for_runtime_write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("nested chat should not bootstrap runtime state")
        ),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(argv)

    assert exc_info.value.code == 1
    assert calls == []
    assert "root Meridian process" in capsys.readouterr().err


def test_config_help_mentions_meridian_toml() -> None:
    assert "meridian.toml" in cli_main.config_app.help
    assert ".meridian/config.toml" not in cli_main.config_app.help


def test_workspace_help_mentions_workspace_local_toml() -> None:
    assert "workspace.local.toml" in cli_main.workspace_app.help
    assert "workspace.toml" not in cli_main.workspace_app.help


def test_init_help_mentions_link_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["init", "--help"])

    assert exc_info.value.code == 0
    assert "--link" in capsys.readouterr().out


def test_init_alias_link_uses_mars_init_when_mars_toml_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_project_root: dict[str, str] = {}
    captured_mars: list[tuple[tuple[str, ...], str | None]] = []

    def _fake_config_init(payload: Any) -> object:
        captured_project_root["value"] = payload.project_root
        return object()

    def _fake_run_mars_passthrough(
        args: list[str] | tuple[str, ...],
        *,
        output_format: str | None = None,
        **_kwargs: object,
    ) -> None:
        captured_mars.append((tuple(args), output_format))

    monkeypatch.setattr(config_ops, "config_init_sync", _fake_config_init)
    monkeypatch.setattr(mars_passthrough, "run_mars_passthrough", _fake_run_mars_passthrough)

    cli_main.init_alias(path=tmp_path.as_posix(), link=".claude")

    expected_root = tmp_path.resolve().as_posix()
    assert captured_project_root["value"] == expected_root
    assert captured_mars == [
        (("--root", expected_root, "init", "--link", ".claude"), "text"),
    ]


def test_init_alias_link_uses_mars_link_when_mars_toml_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_mars: list[tuple[tuple[str, ...], str | None]] = []
    (tmp_path / "mars.toml").write_text("", encoding="utf-8")

    monkeypatch.setattr(config_ops, "config_init_sync", lambda _payload: object())

    def _fake_run_mars_passthrough(
        args: list[str] | tuple[str, ...],
        *,
        output_format: str | None = None,
        **_kwargs: object,
    ) -> None:
        captured_mars.append((tuple(args), output_format))

    monkeypatch.setattr(mars_passthrough, "run_mars_passthrough", _fake_run_mars_passthrough)

    cli_main.init_alias(path=tmp_path.as_posix(), link=".claude")

    expected_root = tmp_path.resolve().as_posix()
    assert captured_mars == [
        (("--root", expected_root, "link", ".claude"), "text"),
    ]


def test_agent_root_help_restricted_surface_contract() -> None:
    for visible in (
        "Meridian is a coordination layer",
        "For automation, use --format json",
        "refs: chat id (c123), spawn id (p123), or raw harness session id",
        "spawn    Create and manage subagent runs",
        "work     Work item dashboard and coordination",
        "config   Show resolved configuration and sources",
        "doctor   Health check and orphan reconciliation",
        "mars     Package management and agent materialization",
    ):
        assert visible in AGENT_ROOT_HELP

    normalized_help = AGENT_ROOT_HELP.lower()
    for hidden in (
        "init",
        "completion",
        "serve",
        "claude",
        "codex",
        "opencode",
    ):
        assert hidden not in normalized_help
    assert AGENT_ROOT_HELP == STARTUP_AGENT_ROOT_HELP


def test_bootstrap_command_enables_bootstrap_documents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "maybe_bootstrap_runtime_state", lambda *_args, **_kwargs: None)

    def _fake_primary_launch(**kwargs: object) -> object:
        captured.update(kwargs)
        return primary_launch.PrimaryLaunchOutput(message="ok", exit_code=0)

    monkeypatch.setattr(primary_launch, "run_primary_launch", _fake_primary_launch)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["bootstrap", "--dry-run"])

    assert exc_info.value.code == 0
    assert captured["include_bootstrap_documents"] is True


def test_bootstrap_command_without_agent_forwards_agent_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "maybe_bootstrap_runtime_state", lambda *_args, **_kwargs: None)

    def _fake_primary_launch(**kwargs: object) -> object:
        captured.update(kwargs)
        return primary_launch.PrimaryLaunchOutput(message="ok", exit_code=0)

    monkeypatch.setattr(primary_launch, "run_primary_launch", _fake_primary_launch)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["bootstrap", "--dry-run"])

    assert exc_info.value.code == 0
    assert captured["agent"] is None
