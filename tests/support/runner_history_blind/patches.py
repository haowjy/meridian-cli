"""Implementation shared by the in-process and subprocess history-blind traps."""

from __future__ import annotations

import builtins
import importlib.abc
import importlib.machinery
import inspect
import io
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def _is_runner_history(path: object) -> bool:
    try:
        parts = Path(path).parts
    except (TypeError, ValueError, OSError):
        return False
    return (
        len(parts) >= 3
        and parts[-1] == "history.jsonl"
        and (
            parts[-3] in {"spawns", "artifacts"}
            or (
                len(parts) >= 4
                and parts[-4] == "spawns"
                and parts[-2].startswith("attempt-")
            )
        )
    )


def _allowed_caller() -> bool:
    for frame in inspect.stack()[2:]:
        if frame.frame.f_globals.get("__name__") in {
            "meridian.lib.state.legacy_history",
            "meridian.lib.state.retention_archive",
        }:
            return True
    return False


def _guard(path: object, reading: bool) -> None:
    if reading and _is_runner_history(path) and not _allowed_caller():
        raise AssertionError(f"runner history read is disabled: {path}")


def _patch_attr(target: object, name: str, value: object, monkeypatch: Any = None) -> None:
    if monkeypatch is None:
        setattr(target, name, value)
    else:
        monkeypatch.setattr(target, name, value)


def _patch_writer_module(name: str, module: ModuleType, monkeypatch: Any = None) -> None:
    """Patch a writer-bearing module once it has finished importing."""
    from meridian.lib.state import atomic, history

    class DirectWriterNoop:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def write(self, *_args: object, **_kwargs: object) -> object:
            return history.WriteResult(success=True, seq=-1)

    class AbsentWriter:
        def __new__(cls, *_args: object, **_kwargs: object) -> None:
            return None

    if name == "meridian.lib.state.history":
        _patch_attr(history, "HarnessHistoryWriter", DirectWriterNoop, monkeypatch)
        _patch_attr(
            history,
            "write_retained_child_stream",
            lambda *_args, **_kwargs: None,
            monkeypatch,
        )
        original_atomic_write = atomic.atomic_write_text

        def atomic_write_text(path: Path, text: str, *args: object, **kwargs: object) -> object:
            if _is_runner_history(path):
                return None
            return original_atomic_write(path, text, *args, **kwargs)

        _patch_attr(atomic, "atomic_write_text", atomic_write_text, monkeypatch)
    elif name in {
        "meridian.lib.streaming.spawn_manager",
        "meridian.lib.launch.process.primary_attach",
    }:
        _patch_attr(module, "HarnessHistoryWriter", AbsentWriter, monkeypatch)


class _WriterPatchLoader(importlib.abc.Loader):
    def __init__(self, loader: Any, fullname: str) -> None:
        self._loader = loader
        self._fullname = fullname

    def create_module(self, spec: Any) -> ModuleType | None:
        create_module = getattr(self._loader, "create_module", None)
        return create_module(spec) if create_module is not None else None

    def exec_module(self, module: ModuleType) -> None:
        self._loader.exec_module(module)
        _patch_writer_module(self._fullname, module)


class _WriterPatchFinder(importlib.abc.MetaPathFinder):
    _TARGETS = frozenset({
        "meridian.lib.state.history",
        "meridian.lib.streaming.spawn_manager",
        "meridian.lib.launch.process.primary_attach",
    })

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: ModuleType | None = None,
    ) -> Any:
        if fullname not in self._TARGETS:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is not None and spec.loader is not None:
            spec.loader = _WriterPatchLoader(spec.loader, fullname)
        return spec


def install_writer_import_hook() -> None:
    """Patch writer modules only when a Meridian process imports them."""
    if not any(isinstance(finder, _WriterPatchFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _WriterPatchFinder())


def install_runner_history_blind(
    monkeypatch: Any = None,
    *,
    patch_writers: bool = True,
) -> None:
    """Disable writer construction and reject implicit history stream reads."""
    original_builtin_open = builtins.open

    def guarded_open(file: object, mode: str = "r", *args: object, **kwargs: object) -> object:
        _guard(file, any(flag in mode for flag in "r+"))
        return original_builtin_open(file, mode, *args, **kwargs)

    original_io_open = io.open

    def guarded_io_open(file: object, mode: str = "r", *args: object, **kwargs: object) -> object:
        _guard(file, any(flag in mode for flag in "r+"))
        return original_io_open(file, mode, *args, **kwargs)

    original_path_open = Path.open

    def guarded_path_open(self: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        _guard(self, any(flag in mode for flag in "r+"))
        return original_path_open(self, mode, *args, **kwargs)

    _patch_attr(builtins, "open", guarded_open, monkeypatch)
    _patch_attr(io, "open", guarded_io_open, monkeypatch)
    _patch_attr(Path, "open", guarded_path_open, monkeypatch)

    if not patch_writers:
        return

    from meridian.lib.state import history
    _patch_writer_module("meridian.lib.state.history", history, monkeypatch)

    from meridian.lib.launch.process import primary_attach
    from meridian.lib.streaming import spawn_manager

    _patch_writer_module("meridian.lib.launch.process.primary_attach", primary_attach, monkeypatch)
    _patch_writer_module("meridian.lib.streaming.spawn_manager", spawn_manager, monkeypatch)
