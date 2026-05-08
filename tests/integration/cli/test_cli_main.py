import importlib
import os
from pathlib import Path
from typing import Any

import pytest

cli_main = importlib.import_module("meridian.cli.main")
mars_passthrough = importlib.import_module("meridian.cli.mars_passthrough")
primary_launch = importlib.import_module("meridian.cli.primary_launch")
config_ops = importlib.import_module("meridian.lib.ops.config")


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


def test_workspace_unknown_subcommand_help_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["workspace", "migrate", "--help"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: Unknown command: workspace migrate\n"


@pytest.mark.parametrize(
    ("argv", "expects_override"),
    [
        (["hooks", "list"], True),
        (["hooks", "run", "record-finalized"], True),
        (["hooks", "check"], False),
    ],
)
def test_hooks_bootstrap_authority_override_applies_only_to_list_and_run(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expects_override: bool,
) -> None:
    parent_project_dir = "/tmp/parent-project"
    parent_runtime_dir = "/tmp/parent-runtime"
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", parent_project_dir)
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", parent_runtime_dir)
    captured_env: list[tuple[str | None, str | None]] = []

    def _fake_bootstrap(*_args: object, **_kwargs: object) -> Path | None:
        captured_env.append(
            (
                os.environ.get("MERIDIAN_PROJECT_DIR"),
                os.environ.get("MERIDIAN_RUNTIME_DIR"),
            )
        )
        return None

    monkeypatch.setattr(cli_main, "maybe_bootstrap_runtime_state", _fake_bootstrap)
    monkeypatch.setattr(cli_main, "_register_commands_for_invocation", lambda *_a, **_k: None)
    monkeypatch.setattr(
        cli_main,
        "_emit_usage_command_invoked",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(cli_main, "app", lambda _argv: None)

    cli_main.main(argv)

    assert captured_env == [
        (
            None if expects_override else parent_project_dir,
            None if expects_override else parent_runtime_dir,
        )
    ]
    assert os.environ.get("MERIDIAN_PROJECT_DIR") == parent_project_dir
    assert os.environ.get("MERIDIAN_RUNTIME_DIR") == parent_runtime_dir
