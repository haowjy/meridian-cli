import importlib
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
