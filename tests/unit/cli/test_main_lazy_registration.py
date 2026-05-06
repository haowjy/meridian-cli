"""Tests for selective lazy command-group registration in ``meridian.cli.main``."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager

import pytest


@contextmanager
def _isolated_meridian_modules() -> Iterator[None]:
    saved = {
        module_name: module
        for module_name, module in sys.modules.items()
        if module_name == "meridian" or module_name.startswith("meridian.")
    }
    for module_name in tuple(saved):
        del sys.modules[module_name]
    try:
        yield
    finally:
        for module_name in tuple(sys.modules):
            if module_name == "meridian" or module_name.startswith("meridian."):
                del sys.modules[module_name]
        sys.modules.update(saved)


@contextmanager
def _fresh_main_module() -> Iterator[object]:
    with _isolated_meridian_modules():
        main = importlib.import_module("meridian.cli.main")
        main._registered_command_groups.clear()
        main._group_commands_registered = False
        yield main


def test_register_commands_for_spawn_imports_spawn_and_report_only() -> None:
    with _fresh_main_module() as main:
        assert "meridian.cli.spawn" not in sys.modules
        assert "meridian.cli.report_cmd" not in sys.modules

        main._register_commands_for_invocation(["spawn", "list"], agent_mode=False)

        assert "meridian.cli.spawn" in sys.modules
        assert "meridian.cli.report_cmd" in sys.modules
        assert "meridian.cli.session_cmd" not in sys.modules
        assert "meridian.cli.work_cmd" not in sys.modules
        assert "meridian.cli.config_cmd" not in sys.modules


@pytest.mark.parametrize(
    ("argv", "expected_module"),
    [
        (["config", "show"], "meridian.cli.config_cmd"),
        (["hooks", "list"], "meridian.cli.hooks_commands"),
        (["telemetry", "status"], "meridian.cli.telemetry_cmd"),
        (["workspace", "list"], "meridian.cli.workspace_cmd"),
        (["completion", "bash"], "meridian.cli.misc_commands"),
        (["chat", "ls"], "meridian.cli.chat_cmd"),
        (["bootstrap", "--dry-run"], "meridian.cli.bootstrap_cmd"),
    ],
)
def test_register_commands_for_invocation_imports_only_needed_group(
    argv: list[str],
    expected_module: str,
) -> None:
    with _fresh_main_module() as main:
        assert expected_module not in sys.modules

        main._register_commands_for_invocation(argv, agent_mode=False)

        assert expected_module in sys.modules
        assert "meridian.cli.spawn" not in sys.modules
        assert "meridian.cli.session_cmd" not in sys.modules


def test_register_commands_for_help_registers_all_command_groups() -> None:
    with _fresh_main_module() as main:
        main._register_commands_for_invocation(["--help"], agent_mode=False)

        for module_name in (
            "meridian.cli.spawn",
            "meridian.cli.session_cmd",
            "meridian.cli.work_cmd",
            "meridian.cli.config_cmd",
            "meridian.cli.hooks_commands",
            "meridian.cli.models_cmd",
            "meridian.cli.ext_cmd",
            "meridian.cli.telemetry_cmd",
            "meridian.cli.workspace_cmd",
            "meridian.cli.doctor_cmd",
            "meridian.cli.bootstrap_cmd",
            "meridian.cli.misc_commands",
            "meridian.cli.chat_cmd",
            "meridian.cli.report_cmd",
            "meridian.cli.kg_cmd",
            "meridian.cli.mermaid_cmd",
        ):
            assert module_name in sys.modules
