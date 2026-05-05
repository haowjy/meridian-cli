"""Tests for startup-local lazy Cyclopts command assembly."""

import importlib
import sys
import types
from collections.abc import Callable

import pytest

from meridian.cli.startup.cyclopts_app import build_lazy_app
from meridian.cli.startup.lazy_dispatch import make_lazy_command


def test_make_lazy_command_for_main_root_returns_callable() -> None:
    handler = make_lazy_command("meridian.cli.main:root")

    assert callable(handler)


def test_lazy_command_does_not_import_target_until_called(monkeypatch: pytest.MonkeyPatch) -> None:
    imported: list[str] = []
    def _fake_command(value: str) -> str:
        return f"handled {value}"

    fake_module = types.SimpleNamespace(group=types.SimpleNamespace(command=_fake_command))

    def _fake_import_module(module_path: str) -> object:
        imported.append(module_path)
        return fake_module

    monkeypatch.setattr(importlib, "import_module", _fake_import_module)

    handler = make_lazy_command("example.lazy:group.command")

    assert imported == []
    assert handler("payload") == "handled payload"
    assert imported == ["example.lazy"]


def test_lazy_command_resolves_to_target_function(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _target(*args: object, **kwargs: object) -> str:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "result"

    fake_module = types.SimpleNamespace(root=_target)

    def _fake_import_module(module_path: str) -> object:
        return fake_module

    monkeypatch.setattr(importlib, "import_module", _fake_import_module)

    handler = make_lazy_command("meridian.cli.main:root")

    assert handler("arg", flag=True) == "result"
    assert captured == {"args": ("arg",), "kwargs": {"flag": True}}


def test_build_lazy_app_does_not_import_command_modules() -> None:
    modules_to_check = ("meridian.cli.spawn", "meridian.cli.chat_cmd")
    originals: dict[str, types.ModuleType] = {}
    for module_name in modules_to_check:
        original = sys.modules.pop(module_name, None)
        if original is not None:
            originals[module_name] = original

    try:
        app = build_lazy_app()

        assert isinstance(app, Callable)
        for module_name in modules_to_check:
            assert module_name not in sys.modules
    finally:
        sys.modules.update(originals)


def test_lazy_command_rejects_invalid_target() -> None:
    handler = make_lazy_command("not-a-valid-target")

    with pytest.raises(ValueError, match=r"module\.path:function\.path"):
        handler()
