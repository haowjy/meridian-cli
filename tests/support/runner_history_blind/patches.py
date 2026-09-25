"""Implementation shared by the in-process and subprocess history-blind traps."""

from __future__ import annotations

import builtins
import inspect
import io
from pathlib import Path
from typing import Any


def _is_runner_history(path: object) -> bool:
    try:
        parts = Path(path).parts
    except (TypeError, ValueError, OSError):
        return False
    return (
        len(parts) >= 3
        and parts[-1] == "history.jsonl"
        and parts[-3]
        in {
            "spawns",
            "artifacts",
        }
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


def install_runner_history_blind(monkeypatch: Any = None) -> None:
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

    from meridian.lib.state import atomic, history

    class DirectWriterNoop:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def write(self, *_args: object, **_kwargs: object) -> object:
            return history.WriteResult(success=True, seq=-1)

    class AbsentWriter:
        def __new__(cls, *_args: object, **_kwargs: object) -> None:
            return None

    _patch_attr(history, "HarnessHistoryWriter", DirectWriterNoop, monkeypatch)
    _patch_attr(history, "write_retained_child_stream", lambda *_args, **_kwargs: None, monkeypatch)
    original_atomic_write = atomic.atomic_write_text

    def atomic_write_text(path: Path, text: str, *args: object, **kwargs: object) -> object:
        if _is_runner_history(path):
            return None
        return original_atomic_write(path, text, *args, **kwargs)

    _patch_attr(atomic, "atomic_write_text", atomic_write_text, monkeypatch)

    from meridian.lib.launch.process import primary_attach
    from meridian.lib.streaming import spawn_manager

    _patch_attr(spawn_manager, "HarnessHistoryWriter", AbsentWriter, monkeypatch)
    _patch_attr(primary_attach, "HarnessHistoryWriter", AbsentWriter, monkeypatch)
