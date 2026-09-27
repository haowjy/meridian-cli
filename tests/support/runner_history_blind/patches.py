"""Read trap shared by the in-process and subprocess ``--runner-history=off`` modes.

Meridian no longer writes runner ``history.jsonl``; legacy files remain on disk.
The trap fails any implicit read of one so a reintroduced runner-stream reader
cannot pass the suite. Legacy archive inventory may still hash the bytes.
"""

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
    return any(
        frame.frame.f_globals.get("__name__") == "meridian.lib.state.retention_archive"
        for frame in inspect.stack()[2:]
    )


def _guard(path: object, reading: bool) -> None:
    if reading and _is_runner_history(path) and not _allowed_caller():
        raise AssertionError(f"runner history read is disabled: {path}")


def _patch_attr(target: object, name: str, value: object, monkeypatch: Any = None) -> None:
    if monkeypatch is None:
        setattr(target, name, value)
    else:
        monkeypatch.setattr(target, name, value)


def install_runner_history_blind(monkeypatch: Any = None) -> None:
    """Reject implicit runner history stream reads."""
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
