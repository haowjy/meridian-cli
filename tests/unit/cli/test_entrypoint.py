"""Tests for the startup-cheap CLI entrypoint fast paths."""

import sys
import types

import pytest

import meridian.cli.entrypoint as entrypoint
from meridian.cli.entrypoint import (
    _is_root_help_request,
    _is_version_request,
    _validate_root_mode_flags,
)
from meridian.cli.startup.help import detect_agent_mode, render_root_help


def test_root_help_request_detects_plain_help() -> None:
    assert _is_root_help_request(["--help"])
    assert _is_root_help_request(["-h"])


def test_root_help_request_skips_global_flag_values() -> None:
    assert _is_root_help_request(["--format", "json", "--help"])
    assert _is_root_help_request(["--model", "gptmini", "-h"])


def test_root_help_request_rejects_command_help() -> None:
    assert not _is_root_help_request(["spawn", "--help"])
    assert not _is_root_help_request(["--format", "json", "spawn", "-h"])


def test_version_request_detects_root_version() -> None:
    assert _is_version_request(["--version"])
    assert _is_version_request(["--format", "json", "--version"])


def test_version_request_rejects_command_version() -> None:
    assert not _is_version_request(["spawn", "--version"])


def test_validate_root_mode_flags_rejects_agent_and_human_together(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit, match="1"):
        _validate_root_mode_flags(["--agent", "--human", "--help"])
    assert "Cannot combine --agent with --human." in capsys.readouterr().err


def test_render_root_help_agent_mode_matches_agent_help_shape() -> None:
    rendered = render_root_help(agent_mode=True)

    assert rendered.startswith("Usage: meridian COMMAND [ARGS]\n")
    assert "For automation, use --format json" in rendered
    assert "meridian spawn -m MODEL --prompt-file /tmp/task.md --bg" in rendered
    assert "Commands:\n  spawn" in rendered


def test_render_root_help_human_mode_has_expected_sections() -> None:
    rendered = render_root_help(agent_mode=False)

    assert rendered.startswith("Usage: meridian [ARGS] [COMMAND]\n")
    assert "Options:" in rendered
    assert "Commands:" in rendered
    assert "Primary launch/resume:" in rendered
    assert "Bundled package manager: meridian mars ARGS..." in rendered
    assert "Run \"meridian spawn -h\" for subagent usage." in rendered


def test_detect_agent_mode_force_flags_override_environment() -> None:
    assert detect_agent_mode(force_agent=True) is True
    assert detect_agent_mode(force_human=True) is False


def test_main_root_help_fast_path_does_not_import_full_main(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = sys.modules.pop("meridian.cli.main", None)
    monkeypatch.setattr(sys, "argv", ["meridian", "--help"])

    try:
        entrypoint.main()
        assert "Usage: meridian [ARGS] [COMMAND]" in capsys.readouterr().out
        assert "meridian.cli.main" not in sys.modules
    finally:
        if original is not None:
            sys.modules["meridian.cli.main"] = original


def test_main_root_version_fast_path_does_not_import_full_main(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = sys.modules.pop("meridian.cli.main", None)
    monkeypatch.setattr(sys, "argv", ["meridian", "--version"])

    try:
        entrypoint.main()
        assert capsys.readouterr().out.startswith("meridian ")
        assert "meridian.cli.main" not in sys.modules
    finally:
        if original is not None:
            sys.modules["meridian.cli.main"] = original


def test_main_delegates_non_fast_paths_to_full_main(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    fake_main_module = types.ModuleType("meridian.cli.main")

    def _fake_main(*, argv: list[str]) -> None:
        captured["argv"] = argv

    fake_main_module.main = _fake_main  # type: ignore[attr-defined]

    original = sys.modules.get("meridian.cli.main")
    sys.modules["meridian.cli.main"] = fake_main_module
    monkeypatch.setattr(sys, "argv", ["meridian", "spawn", "list"])

    try:
        entrypoint.main()
    finally:
        if original is not None:
            sys.modules["meridian.cli.main"] = original
        else:
            sys.modules.pop("meridian.cli.main", None)

    assert captured == {"argv": ["spawn", "list"]}
